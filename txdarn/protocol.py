'''Core protocol implementation.

Classes that use automat's MethodicalMachine can be visualized!

Here's how to do it:

python -c 'import sys, txdarn.protocol as P; \
           sys.stdout.writelines(P.RequestSessionMachine._machine.graphviz)' \
      | dot -Tpng > machine.png
'''

import txaio
txaio.use_twisted()

from autobahn.websocket.protocol import WebSocketProtocol
from autobahn.twisted.websocket import (WrappingWebSocketServerFactory,
                                        WrappingWebSocketServerProtocol)
from automat import MethodicalMachine

from constantly import Values, ValueConstant

import eliot

from twisted.internet import defer
from twisted.internet import reactor, protocol, error
from twisted.python import failure
from twisted.protocols.policies import ProtocolWrapper, WrappingFactory

from zope.interface import directlyProvides, providedBy

from txdarn.compat import asJSON, fromJSON


class TxDarnProtocolException(Exception):
    '''Raised when a protocol-level error occurs'''


def _trapCancellation(failure):
    failure.trap(defer.CancelledError)


def sockJSJSON(data, cls=None):
    # no spaces
    return asJSON(data, separators=(',', ':'), cls=cls)


class INVALID_DATA(Values):
    NO_PAYLOAD = ValueConstant(b'Payload expected.\n')
    BAD_JSON = ValueConstant(b'Broken JSON encoding.\n')


class DISCONNECT(Values):
    GO_AWAY = ValueConstant([3000, "Go away!"])
    STILL_OPEN = ValueConstant([2010, "Another connection still open"])


class HeartbeatClock(object):
    '''Schedules a recurring heartbeat frame, but only if no data has
    been written recently.

    '''

    writeHeartbeat = None
    pendingHeartbeat = None
    stopped = False

    def __init__(self, writeHeartbeat=None, period=25.0, clock=reactor):
        self.writeHeartbeat = writeHeartbeat
        self.period = period
        self.clock = clock

    def _createHeartbeatCall(self):
        self.pendingHeartbeat = self.clock.callLater(self.period,
                                                     self._sendHeartbeat)

    def _sendHeartbeat(self):
        self.writeHeartbeat()
        self._createHeartbeatCall()

    def schedule(self):
        """Schedule or reschedule the next heartbeat."""
        if self.stopped:
            raise RuntimeError("Can't schedule stopped heartbeat")

        if self.pendingHeartbeat is None:
            self._createHeartbeatCall()
        else:
            self.pendingHeartbeat.reset(self.period)

    def stop(self):
        """Permanently stop sending heartbeats."""
        if not self.stopped:
            self.stopped = True
            if self.pendingHeartbeat is not None:
                self.pendingHeartbeat.cancel()
                self.pendingHeartbeat = None


class SockJSProtocolMachine(object):
    _machine = MethodicalMachine()
    transport = None

    def __init__(self, heartbeater):
        self.heartbeater = heartbeater

    @classmethod
    def withHeartbeater(cls, heartbeater):
        """Connect a SockJSProtocolMachine to its heartbeater."""
        instance = cls(heartbeater)
        heartbeater.writeHeartbeat = instance.heartbeat
        return instance

    @_machine.state(initial=True)
    def notYetConnected(self):
        '''A connection has not yet been made.'''

    @_machine.state()
    def connected(self):
        '''We have a connection!'''

    @_machine.state()
    def disconnected(self):
        '''We have been disconnected.'''

    @_machine.input()
    def connect(self, transport):
        '''Establish a connection on the transport.'''

    @_machine.output()
    def _connectionEstablished(self, transport):
        '''Store a reference to our transport and write an open frame.'''
        self.transport = transport
        self.transport.writeOpen()
        self.heartbeater.schedule()

    @_machine.input()
    def write(self, data):
        '''We should write an array-like thing to the transport.'''

    @_machine.output()
    def _writeToTransport(self, data):
        '''Frame the array-like thing and write it.'''
        self.transport.writeData(data)
        self.heartbeater.schedule()

    @_machine.input()
    def receive(self, data):
        '''Data has arrived!'''

    @_machine.output()
    def _received(self, data):
        """Receive data -- just for completeness' sake"""
        return data

    @_machine.input()
    def heartbeat(self):
        '''Time to send a heartbeat.'''

    @_machine.output()
    def _writeHeartbeatToTransport(self):
        '''Write a heartbeat frame'''
        self.transport.writeHeartbeat()

    @_machine.input()
    def disconnect(self, reason=DISCONNECT.GO_AWAY):
        '''We're closing the connection because of reason.'''

    @_machine.input()
    def close(self):
        '''Our connection has been closed'''

    @_machine.output()
    def _writeCloseFrame(self, reason=DISCONNECT.GO_AWAY):
        '''Write a close frame with the given reason and schedule this
        connection close.

        '''
        self.transport.writeClose(reason)
        self.transport.loseConnection()
        self.transport = None

    @_machine.output()
    def _stopHeartbeatWithReason(self, reason=DISCONNECT.GO_AWAY):
        '''We lost our connection - stop our heartbeat.  This runs when the
        protocol wants a disconnection.

        This redundant output ensures that the heartbeater stops
        immediately after the connection is lost.  Twisted will call
        connectionLost after loseConnection, but between the two the
        heartbeat-writing callback may run, after the transport's been
        lost.  Relying on connectionLost to cancel our
        callback thus creates a race condition.

        '''
        self.heartbeater.stop()
        self.heartbeater = None

    @_machine.output()
    def _stopHeartbeat(self):
        '''We lost our connection - stop our heartbeat.  This runs when a
        connection is lost.

        '''
        self.transport = None
        self.heartbeater.stop()
        self.heartbeater = None

    notYetConnected.upon(connect,
                         enter=connected,
                         outputs=[_connectionEstablished])
    notYetConnected.upon(disconnect,
                         enter=disconnected,
                         outputs=[])

    connected.upon(write,
                   enter=connected,
                   outputs=[_writeToTransport])
    connected.upon(receive,
                   enter=connected,
                   outputs=[_received],
                   collector=lambda x: next(iter(x)))

    connected.upon(heartbeat,
                   enter=connected,
                   outputs=[_writeHeartbeatToTransport])
    connected.upon(disconnect,
                   enter=disconnected,
                   outputs=[_writeCloseFrame,
                            _stopHeartbeatWithReason])

    connected.upon(close,
                   enter=disconnected,
                   outputs=[_stopHeartbeat])

    # close should be idempotent
    disconnected.upon(close,
                      enter=disconnected,
                      outputs=[])


class InvalidData(TxDarnProtocolException):
    '''Received invalid JSON.'''

    def __init__(self, reason):
        super(InvalidData, self).__init__(
            "Could not decode data: {!r}".format(reason))
        self.reason = reason


class SockJSWireProtocolWrapper(ProtocolWrapper):
    '''Serialize and deserialize SockJS protocol elements.  Used as a
    base class for various transports.

    '''

    def __init__(self, factory, wrappedProtocol):
        ProtocolWrapper.__init__(self, factory, wrappedProtocol)
        self.jsonDecoder = self.factory.jsonDecoder
        self.jsonEncoder = self.factory.jsonEncoder

    def jsonReceived(self, decoded):
        self.wrappedProtocol.dataReceived(decoded)

    def dataReceived(self, data):
        if not data:
            raise InvalidData(INVALID_DATA.NO_PAYLOAD.value)
        else:
            try:
                decoded = fromJSON(data, cls=self.jsonDecoder)
            except ValueError:
                raise InvalidData(INVALID_DATA.BAD_JSON.value)
            else:
                self.jsonReceived(decoded)

    def writeOpen(self):
        '''Write an open frame.'''
        self.write(b'o')

    def writeHeartbeat(self):
        self.write(b'h')

    def closeFrame(self, reason):
        frameValue = [b'c',
                      sockJSJSON(reason.value, cls=self.jsonEncoder)]
        return b''.join(frameValue)

    def writeClose(self, reason):
        self.write(self.closeFrame(reason))

    def writeData(self, data):
        frameValue = [b'a', sockJSJSON(data, cls=self.jsonEncoder)]
        data = b''.join(frameValue)
        self.write(data)


class SockJSWireProtocolWrappingFactory(WrappingFactory):
    '''Factory that wraps a transport with SockJSWireProtocolWrapper.
    Used by transport factories (e.g., HTTP request, websocket
    connection).

    '''
    protocol = SockJSWireProtocolWrapper

    def __init__(self, wrappedFactory, jsonEncoder=None, jsonDecoder=None):
        WrappingFactory.__init__(self, wrappedFactory)
        self.jsonEncoder = jsonEncoder
        self.jsonDecoder = jsonDecoder


class SockJSProtocol(ProtocolWrapper):
    '''Wrap a user-supplied protocol for use with SockJS.'''

    def __init__(self, factory, wrappedProtocol):
        ProtocolWrapper.__init__(self, factory, wrappedProtocol)
        self.sockJSMachine = self.factory.stateMachineFactory()

    def connectionMade(self):
        # don't catch any exception here - we want to stop
        # ProtocolWrapper.makeConnection from calling
        # self.wrappedProtocol.makeConnection
        self.sockJSMachine.connect(self.transport)

    def dataReceived(self, data):
        data = self.sockJSMachine.receive(data)
        self.wrappedProtocol.dataReceived(data)

    def write(self, data):
        self.sockJSMachine.write(data)

    def writeSequence(self, data):
        for datum in data:
            self.sockJSMachine.write(datum)

    def loseConnection(self):
        self.sockJSMachine.disconnect()

    def connectionLost(self, reason=protocol.connectionDone):
        self.sockJSMachine.close()
        self.sockJSMachine = None
        self.wrappedProtocol.connectionLost(reason)


class SockJSProtocolFactory(WrappingFactory):
    """Factory that wraps another factory to provide the SockJS protocol.

    Wrap your protocol's factory with this for use with TxDarnApp.

    """

    protocol = SockJSProtocol

    def __init__(self, wrappedFactory, heartbeatPeriod=25.0, clock=reactor):
        WrappingFactory.__init__(self, wrappedFactory)
        self.heartbeatPeriod = heartbeatPeriod
        self.clock = clock

    def stateMachineFactory(self):
        heartbeater = HeartbeatClock(period=self.heartbeatPeriod)
        return SockJSProtocolMachine.withHeartbeater(heartbeater)


class SessionTimeout(Exception):
    """A session has timed out before all its data has been written."""


class RequestSessionMachine(object):
    _machine = MethodicalMachine()
    _closeReason = None

    def __init__(self, requestSession):
        self.buffer = []
        self.requestSession = requestSession

    @_machine.state(initial=True)
    def neverConnected(self):
        '''We've never been connected to any request.'''

    @_machine.state()
    def connectedHaveTransport(self):
        '''We're attached to a transport.'''

    @_machine.state()
    def connectedNoTransportEmptyBuffer(self):
        '''We've been detached from our request and have no pending data to
        write.

        '''

    @_machine.state()
    def connectedNoTransportPending(self):
        '''We've been detached from our request but there's pending data.'''

    @_machine.state()
    def loseConnectionEmptyBuffer(self):
        '''We were told to lose the connection, and we have a request and thus
    an empty buffer.

        '''

    @_machine.state()
    def loseConnectionPending(self):
        '''We were told to lose the connection, but we have data still in our
        buffer.

        '''

    @_machine.state()
    def disconnected(self):
        '''The session bound to this protocol's lifetime has disappeared.'''

    @_machine.input()
    def attach(self, request):
        '''Attach to the request, performing setup if necessary.  The
        user-visible return value of this input should be True if the
        request attached and False if not, because an existing request
        was already attached.

        '''

    @_machine.input()
    def detach(self):
        '''Detach the current request'''

    @_machine.input()
    def write(self, data):
        '''The protocol wants to write to the transport.'''

    @_machine.input()
    def receive(self, data):
        '''The underlying transport wants to us to receive some data.'''

    @_machine.input()
    def writeClose(self, reason):
        '''The protocol wants to close the session for reason'''

    @_machine.input()
    def heartbeat(self):
        '''The protocol wants to send a heartbeat.'''

    @_machine.input()
    def loseConnection(self):
        '''Lose the request, if applicable.'''

    @_machine.input()
    def connectionLost(self, reason=protocol.connectionDone):
        '''The connection has been lost; clean up any request and clean up the
        protocol.

        '''

    @_machine.output()
    def _openRequest(self, request):
        assert self.requestSession.request is None
        self.requestSession.request = request

    @_machine.output()
    def _establishConnection(self, request):
        self.requestSession.establishConnection(request)

    @_machine.output()
    def _completeConnection(self, request):
        self.requestSession.completeConnection()

    @_machine.output()
    def _beginRequest(self, request):
        self.requestSession.beginRequest()

    @_machine.output()
    def _completeDataReceived(self, data):
        self.requestSession.completeDataReceived(data)

    @_machine.output()
    def _bufferWrite(self, data):
        '''Without a request, we have to buffer our writes.'''
        self.buffer.extend(data)

    @_machine.output()
    def _flushBuffer(self, request):
        '''Flush any pending data from the buffer to the request'''
        assert request is self.requestSession.request
        self.requestSession.writeData(self.buffer)
        self.buffer = []

    @_machine.output()
    def _dumpBuffer(self):
        '''Forget the contents of the buffer.'''
        self.buffer = []

    @_machine.output()
    def _directWrite(self, data):
        '''Skip our buffer'''
        self.requestSession.completeWrite(data)

    @_machine.output()
    def _directHeartbeat(self):
        self.requestSession.completeHeartbeat()

    @_machine.output()
    def _closeRequest(self):
        self.requestSession.finishCurrentRequest()

    @_machine.output()
    def _closeDuplicateRequest(self, request):
        if request is not self.requestSession.request:
            self.requestSession.closeOtherRequest(request,
                                                  DISCONNECT.STILL_OPEN)

    @_machine.output()
    def _loseConnection(self):
        self.requestSession.completeLoseConnection()

    @_machine.output()
    def _storeCloseReason(self, reason):
        self._closeReason = reason

    @_machine.output()
    def _writeCloseReason(self, request):
        if self._closeReason:
            self.requestSession.closeOtherRequest(request,
                                                  self._closeReason)

    @_machine.output()
    def _dropRequest(self, reason=protocol.connectionDone):
        self.requestSession.request = None

    @_machine.output()
    def _closeProtocol(self, reason=protocol.connectionDone):
        self.requestSession.completeConnectionLost(reason)
        self.requestSession = None

    @_machine.output()
    def _timedOut(self, reason=protocol.connectionDone):
        if isinstance(reason.value, error.ConnectionDone):
            reason = failure.Failure(SessionTimeout())
        self.requestSession.completeConnectionLost(reason=reason)
        self.requestSession = None

    neverConnected.upon(attach,
                        enter=connectedHaveTransport,
                        outputs=[_openRequest,
                                 _establishConnection,
                                 _beginRequest,
                                 _completeConnection])

    connectedHaveTransport.upon(write,
                                enter=connectedHaveTransport,
                                outputs=[_directWrite])
    connectedHaveTransport.upon(receive,
                                enter=connectedHaveTransport,
                                outputs=[_completeDataReceived])
    connectedHaveTransport.upon(heartbeat,
                                enter=connectedHaveTransport,
                                outputs=[_directHeartbeat])
    connectedHaveTransport.upon(detach,
                                enter=connectedNoTransportEmptyBuffer,
                                outputs=[_closeRequest])
    connectedHaveTransport.upon(attach,
                                enter=connectedHaveTransport,
                                outputs=[_closeDuplicateRequest])
    # XXX this should transition to a new state: expectLoseConnection.
    connectedHaveTransport.upon(writeClose,
                                enter=connectedHaveTransport,
                                outputs=[_storeCloseReason])
    connectedHaveTransport.upon(loseConnection,
                                enter=loseConnectionEmptyBuffer,
                                outputs=[_closeRequest,
                                         _loseConnection])
    connectedHaveTransport.upon(connectionLost,
                                enter=disconnected,
                                outputs=[_dropRequest,
                                         _closeProtocol])

    connectedNoTransportEmptyBuffer.upon(write,
                                         enter=connectedNoTransportPending,
                                         outputs=[_bufferWrite])
    connectedNoTransportEmptyBuffer.upon(receive,
                                         enter=connectedNoTransportEmptyBuffer,
                                         outputs=[_completeDataReceived])
    connectedNoTransportEmptyBuffer.upon(heartbeat,
                                         enter=connectedNoTransportEmptyBuffer,
                                         outputs=[])
    connectedNoTransportEmptyBuffer.upon(attach,
                                         enter=connectedHaveTransport,
                                         outputs=[_openRequest,
                                                  _beginRequest])
    connectedNoTransportEmptyBuffer.upon(detach,
                                         enter=connectedNoTransportEmptyBuffer,
                                         outputs=[])
    # XXX this should transition to a new state: expectConnectionLost
    connectedNoTransportEmptyBuffer.upon(writeClose,
                                         enter=connectedNoTransportEmptyBuffer,
                                         outputs=[_storeCloseReason])
    connectedNoTransportEmptyBuffer.upon(loseConnection,
                                         enter=loseConnectionEmptyBuffer,
                                         outputs=[_loseConnection])
    connectedNoTransportEmptyBuffer.upon(connectionLost,
                                         enter=disconnected,
                                         outputs=[_closeProtocol])

    loseConnectionEmptyBuffer.upon(attach,
                                   enter=loseConnectionEmptyBuffer,
                                   outputs=[_writeCloseReason])
    loseConnectionEmptyBuffer.upon(receive,
                                   enter=loseConnectionEmptyBuffer,
                                   outputs=[])
    loseConnectionEmptyBuffer.upon(connectionLost,
                                   enter=disconnected,
                                   outputs=[_closeProtocol])

    connectedNoTransportPending.upon(write,
                                     enter=connectedNoTransportPending,
                                     outputs=[_bufferWrite])
    connectedNoTransportPending.upon(receive,
                                     enter=connectedNoTransportPending,
                                     outputs=[_completeDataReceived])
    connectedNoTransportPending.upon(heartbeat,
                                     enter=connectedNoTransportPending,
                                     outputs=[])
    connectedNoTransportPending.upon(attach,
                                     enter=connectedHaveTransport,
                                     outputs=[_openRequest,
                                              _flushBuffer])
    connectedNoTransportPending.upon(detach,
                                     enter=connectedNoTransportPending,
                                     outputs=[])
    connectedNoTransportPending.upon(writeClose,
                                     enter=connectedNoTransportPending,
                                     outputs=[_storeCloseReason])
    connectedNoTransportPending.upon(loseConnection,
                                     enter=loseConnectionPending,
                                     outputs=[_dumpBuffer,
                                              _loseConnection])
    connectedNoTransportPending.upon(connectionLost,
                                     enter=disconnected,
                                     outputs=[_dropRequest,
                                              _timedOut])

    loseConnectionPending.upon(connectionLost,
                               enter=disconnected,
                               outputs=[_timedOut])
    loseConnectionPending.upon(attach,
                               enter=loseConnectionPending,
                               outputs=[_writeCloseReason])
    loseConnectionPending.upon(receive,
                               enter=loseConnectionPending,
                               outputs=[])
    loseConnectionPending.upon(detach,
                               enter=loseConnectionPending,
                               outputs=[])


class TimeoutClock(object):
    '''
    Expires sessions.
    '''
    EXPIRED = 'EXPIRED'

    expired = False
    timeoutCall = None

    def __init__(self, terminationDeferred, length=5.0, clock=reactor):
        self.length = length
        self.clock = clock
        self.terminationDeferred = terminationDeferred

    def _expire(self):
        self.expired = True
        self.timeoutCall = None
        self.terminationDeferred.callback(self.EXPIRED)

    def reset(self):
        if self.expired:
            raise RuntimeError("Cannot restart expired timeout.")

        if self.timeoutCall is not None:
            self.timeoutCall.cancel()
            self.timeoutCall = None

    def start(self):
        if self.expired:
            raise RuntimeError("Cannot start expired timeout.")

        if not self.expired and self.timeoutCall is None:
            self.timeoutCall = self.clock.callLater(self.length,
                                                    self._expire)

    def stop(self):
        if not self.expired and self.timeoutCall is not None:
            self.timeoutCall.cancel()
            self.timeoutCall = None


class RequestSessionProtocolWrapper(SockJSWireProtocolWrapper):
    """A protocol wrapper that uses an http.Request object as its
    transport.

    The protocol cannot be started without a request, but it may outlive that
    and many subsequent requests.

    This is the base class for polling SockJS transports

    """
    terminationDeferred = None
    request = None
    finishedNotifier = None

    def __init__(self, *args, **kwargs):
        SockJSWireProtocolWrapper.__init__(self, *args, **kwargs)

        self.terminationDeferred = defer.Deferred()
        self.terminationDeferred.addCallback(self._timedOut)

        self.sessionMachine = self.factory.sessionMachineFactory(self)
        self.timeoutClock = self.factory.timeoutClockFactory(
            self.terminationDeferred)

    def makeConnection(self, transport):
        name = self.__class__.__name__
        raise RuntimeError(
            "Do not use {name}.makeConnection;"
            " instead use {name}.makeConnectionFromRequest".format(name=name))

    @property
    def attached(self):
        return self.request is not None

    def makeConnectionFromRequest(self, request):
        self.sessionMachine.attach(request)

    def detachFromRequest(self):
        self.sessionMachine.detach()

    def write(self, data):
        self.request.write(data + b'\n')

    def closeOtherRequest(self, request, reason):
        request.write(self.closeFrame(reason) + b'\n')
        request.finish()

    def dataReceived(self, data):
        self.sessionMachine.receive(data)

    def writeData(self, data):
        self.sessionMachine.write(data)

    def writeHeartbeat(self):
        self.sessionMachine.heartbeat()

    def writeClose(self, reason):
        self.sessionMachine.writeClose(reason)

    def loseConnection(self):
        if not self.disconnecting:
            self.disconnecting = 1
            self.sessionMachine.loseConnection()
            self.timeoutClock.start()

    def connectionLost(self, reason=protocol.connectionDone):
        if not self.disconnecting:
            self.terminationDeferred.errback(reason)
            self.timeoutClock.stop()
        self.sessionMachine.connectionLost(reason)
        self.sessionMachine = None

    def registerProducer(self, producer, streaming):
        # TODO: implement this!
        raise NotImplementedError

    def unregisterProducer(self):
        # TODO: implement this!
        raise NotImplementedError

    def _timedOut(self, timeoutReason):
        # TODO: reconsider this API - setting disconnecting isn't
        # great
        self.disconnecting = 1
        self.connectionLost()

    def beginRequest(self):
        self.finishedNotifier = self.request.notifyFinish()
        self.finishedNotifier.addErrback(_trapCancellation)
        self.finishedNotifier.addErrback(self.connectionLost)
        self.timeoutClock.reset()

    def establishConnection(self, request):
        directlyProvides(self, providedBy(request.transport))
        protocol.Protocol.makeConnection(self, request.transport)
        self.factory.registerProtocol(self)

    def completeConnection(self):
        self.wrappedProtocol.makeConnection(self)

    def completeDataReceived(self, data):
        SockJSWireProtocolWrapper.dataReceived(self, data)

    def completeWrite(self, data):
        SockJSWireProtocolWrapper.writeData(self, data)

    def completeHeartbeat(self):
        SockJSWireProtocolWrapper.writeHeartbeat(self)

    def completeConnectionLost(self, reason):
        SockJSWireProtocolWrapper.connectionLost(self, reason)

    def completeLoseConnection(self):
        SockJSWireProtocolWrapper.loseConnection(self)

    def finishCurrentRequest(self):
        if self.finishedNotifier:
            self.finishedNotifier.cancel()
        self.request.finish()
        self.request = None
        self.finishedNotifier = None
        self.timeoutClock.start()


class RequestSessionWrappingFactory(SockJSWireProtocolWrappingFactory):
    protocol = RequestSessionProtocolWrapper

    def __init__(self, wrappedFactory, timeout=5.0,
                 jsonEncoder=None, jsonDecoder=None):
        SockJSWireProtocolWrappingFactory.__init__(self,
                                                   wrappedFactory,
                                                   jsonEncoder=jsonEncoder,
                                                   jsonDecoder=jsonDecoder)
        self.timeout = timeout

    def sessionMachineFactory(self, protocol):
        return RequestSessionMachine(protocol)

    def timeoutClockFactory(self, terminationDeferred):
        return TimeoutClock(terminationDeferred, self.timeout)


class SessionHouse(object):

    def __init__(self):
        self.sessions = {}

    def validateAndExtractSessionID(self, request):
        try:
            serverID, sessionID, _ = request.postpath
        except ValueError:
            return None

        if any(not identifier or b'.' in identifier
               for identifier in request.postpath):
            return None

        return sessionID

    def makeSession(self, sessionID, factory, request):
        protocol = factory.buildProtocol(request.transport.getPeer())

        protocol.terminationDeferred.addBoth(self._sessionClosed, sessionID)
        protocol.terminationDeferred.addErrback(eliot.writeFailure)

        return protocol

    def _sessionClosed(self, maybeFailure, sessionID):
        del self.sessions[sessionID]
        if isinstance(maybeFailure, failure.Failure):
            failure.trap(error.ConnectionDone,
                         error.ConnectionLost,
                         SessionTimeout)
        else:
            return maybeFailure

    def attachToSession(self, factory, request):
        sessionID = self.validateAndExtractSessionID(request)
        if sessionID is None:
            return False

        session = self.sessions.get(sessionID)
        if not session:
            session = self.makeSession(sessionID, factory, request)
            self.sessions[sessionID] = session

        session.makeConnectionFromRequest(request)
        return True

    def writeToSession(self, request):
        sessionID = self.validateAndExtractSessionID(request)
        if sessionID is None:
            return False
        try:
            session = self.sessions[sessionID]
        except KeyError:
            return False
        else:
            data = request.content.read()
            session.dataReceived(data)
            return True


class XHRSession(RequestSessionProtocolWrapper):

    def writeOpen(self):
        RequestSessionProtocolWrapper.writeOpen(self)
        self.detachFromRequest()

    def writeData(self, data):
        RequestSessionProtocolWrapper.writeData(self, data)
        self.detachFromRequest()


class XHRSessionFactory(RequestSessionWrappingFactory):
    protocol = XHRSession


class XHRStreamingSession(RequestSessionProtocolWrapper):
    prelude = b'h' * 2048
    bytesWritten = 0

    def writeOpen(self):
        self.write(self.prelude)
        RequestSessionProtocolWrapper.writeOpen(self)

    def completeWrite(self, data):
        RequestSessionProtocolWrapper.completeWrite(self, data)
        self.bytesWritten += sum(map(len, data))
        if self.bytesWritten >= self.factory.maximumBytes:
            self.bytesWritten = 0
            self.detachFromRequest()


class XHRStreamingSessionFactory(RequestSessionWrappingFactory):
    protocol = XHRStreamingSession

    def __init__(self, maximumBytes, *args, **kwargs):
        RequestSessionWrappingFactory.__init__(self, *args, **kwargs)
        self.maximumBytes = maximumBytes


class WebSocketProtocolWrapper(SockJSWireProtocolWrapper):

    def jsonReceived(self, decoded):
        if decoded:
            SockJSWireProtocolWrapper.jsonReceived(self, decoded)

    def dataReceived(self, data):
        if not data:
            return
        try:
            SockJSWireProtocolWrapper.dataReceived(self, data)
        except InvalidData:
            self.loseConnection()


class WebSocketWrappingFactory(SockJSWireProtocolWrappingFactory):
    protocol = WebSocketProtocolWrapper


class _WebSocketServerProtocol(WrappingWebSocketServerProtocol):
    '''Autobahn's WrappingWebSocketServerProtocol requires that text
    frames be base64 encoded.  This breaks SockJS (and presumably many
    other things).  Furthermore, it calls the wrapped protocol's
    connectionMade directly rather than calling its makeConnection.
    This class fixes both of these issues.

    '''

    def onConnect(self, request):
        # base64 is not required for text frames
        self._binaryMode = any(b'binary' in p for p in request.protocols)

    def onOpen(self):
        # override default behavior of calling connectionMade directly
        self._proto.makeConnection(self)

    def write(self, data):
        # base64 is not required for text frames
        assert type(data) == bytes
        if self._binaryMode:
            self.sendMessage(data, isBinary=True)
        else:
            self.sendMessage(data, isBinary=False)

    def onMessage(self, payload, isBinary):
        # base64 is not required for text frames
        if isBinary != self._binaryMode:
            self.failConnection(
                WebSocketProtocol.CLOSE_STATUS_CODE_UNSUPPORTED_DATA,
                "message payload type does not match"
                " the negotiated subprotocol")
        else:
            self._proto.dataReceived(payload)


class WebSocketSessionFactory(WrappingWebSocketServerFactory):

    def __init__(self, wrappedFactory, jsonEncoder=None, jsonDecoder=None,
                 reactor=None,
                 enableCompression=True,
                 autoFragmentSize=0,
                 subprotocol=None):

        sockJSWrappedFactory = WebSocketWrappingFactory(
            wrappedFactory)
        WrappingWebSocketServerFactory.__init__(
            self,
            sockJSWrappedFactory,
            url=None,
            reactor=reactor,
            enableCompression=enableCompression,
            autoFragmentSize=autoFragmentSize,
            subprotocol=subprotocol)

    def buildProtocol(self, addr):
        proto = _WebSocketServerProtocol()
        proto.factory = self
        proto._proto = self._factory.buildProtocol(addr)
        proto._proto.transport = proto
        return proto
