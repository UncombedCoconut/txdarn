import io
import json

import automat
import autobahn.websocket.types as A

from twisted.trial import unittest
from twisted.internet import error
from twisted.internet.defer import Deferred, DeferredList
from twisted.internet.protocol import Protocol, Factory, connectionDone
from twisted.test.proto_helpers import StringTransport
from twisted.internet.task import Clock
from twisted.internet.address import IPv4Address
# TODO: don't use twisted's private test APIs
from twisted.web.test.requesthelper import DummyRequest

from zope.interface import Interface, implementer, implementedBy

from .. import protocol as P


class sockJSJSONTestCase(unittest.SynchronousTestCase):

    def test_sockJSJSON(self):
        self.assertEqual(P.sockJSJSON([3000, 'Go away!']),
                         b'[3000,"Go away!"]')


class HeartbeatClockTestCase(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.period = 25.0
        self.heartbeats = 0

        def fakeHeartbeat():
            self.heartbeats += 1

        self.heartbeater = P.HeartbeatClock(fakeHeartbeat,
                                            period=self.period,
                                            clock=self.clock)

    def test_neverScheduled(self):
        '''Heartbeats are not scheduled before their first schedule(), and are
        not scheduled if we immediately stop() the HeartbeatClock.  A
        stopped HeartbeatClock can never schedule any other
        heartbeats.

        '''

        self.assertFalse(self.clock.getDelayedCalls())
        self.heartbeater.stop()
        self.assertFalse(self.clock.getDelayedCalls())

        with self.assertRaises(RuntimeError):
            self.heartbeater.schedule()

        self.assertFalse(self.clock.getDelayedCalls())

    def test_schedule(self):
        '''Heartbeats are scheduled and recur if not interrupted.'''

        self.heartbeater.schedule()
        pendingBeat = self.heartbeater.pendingHeartbeat
        self.assertEqual(self.clock.getDelayedCalls(), [pendingBeat])

        self.clock.advance(self.period * 2)
        self.assertEqual(self.heartbeats, 1)

        rescheduledPendingBeat = self.heartbeater.pendingHeartbeat
        self.assertIsNot(pendingBeat, rescheduledPendingBeat)
        self.assertEqual(self.clock.getDelayedCalls(),
                         [rescheduledPendingBeat])

    def test_schedule_interrupts(self):
        '''A schedule() call will remove the pending heartbeat and reschedule
        it for later.

        '''
        self.heartbeater.schedule()
        pendingBeat = self.heartbeater.pendingHeartbeat
        self.assertEqual(self.clock.getDelayedCalls(), [pendingBeat])

        self.heartbeater.schedule()

        self.assertFalse(self.heartbeats)

        rescheduledPendingBeat = self.heartbeater.pendingHeartbeat
        self.assertEqual(self.clock.getDelayedCalls(),
                         [rescheduledPendingBeat])

    def test_schedule_stop(self):
        '''A stop() call removes any pending heartbeats.'''
        self.heartbeater.schedule()
        pendingBeat = self.heartbeater.pendingHeartbeat
        self.assertEqual(self.clock.getDelayedCalls(), [pendingBeat])

        self.heartbeater.stop()
        self.assertFalse(self.heartbeats)
        self.assertFalse(self.clock.getDelayedCalls())

        # this does not raise an exception
        self.heartbeater.stop()


class TestProtocol(Protocol):
    connectionMadeCalls = 0

    def connectionMade(self):
        self.connectionMadeCalls += 1
        if self.connectionMadeCalls > 1:
            assert False, "connectionMade must only be called once"


class RecordingProtocol(TestProtocol):

    def dataReceived(self, data):
        self.factory.receivedData.append(data)

    def connectionLost(self, reason):
        self.factory.connectionsLost.append(reason)


class RecordingProtocolFactory(Factory):
    protocol = RecordingProtocol

    def __init__(self, receivedData, connectionsLost):
        self.receivedData = receivedData
        self.connectionsLost = connectionsLost


class EchoProtocol(TestProtocol):
    DISCONNECT = 'DISCONNECT'

    def dataReceived(self, data):
        if isinstance(data, list):
            self.transport.writeSequence(data)
        else:
            self.transport.write(data)

    def connectionLost(self, reason):
        self.factory.connectionLost.append(reason)


class EchoProtocolFactory(Factory):
    protocol = EchoProtocol

    def __init__(self, connectionLost):
        self.connectionLost = connectionLost


class SockJSWireProtocolWrapperTestCase(unittest.TestCase):
    '''Sanity tests for SockJS transport base class.'''

    def setUp(self):
        self.transport = StringTransport()

        self.receivedData = []
        self.connectionsLost = []
        self.wrappedFactory = RecordingProtocolFactory(self.receivedData,
                                                       self.connectionsLost)
        self.factory = self.makeFactory()

        self.address = IPv4Address('TCP', '127.0.0.1', 80)

        self.protocol = self.factory.buildProtocol(self.address)
        self.protocol.makeConnection(self.transport)

    def makeFactory(self):
        '''Returns the WrappingFactory for this test case.  Override me in
        subclasses that test different session wrapper protocols.

        '''
        return P.SockJSWireProtocolWrappingFactory(self.wrappedFactory)

    def test_writeOpen(self):
        '''writeOpen writes a single open frame.'''
        self.protocol.writeOpen()
        self.assertEqual(self.transport.value(), b'o')

    def test_writeHeartbeat(self):
        '''writeHeartbeat writes a single heartbeat frame.'''
        self.protocol.writeHeartbeat()
        self.assertEqual(self.transport.value(), b'h')

    def test_writeClose(self):
        '''writeClose writes a close frame containing the provided reason.'''
        self.protocol.writeClose(P.DISCONNECT.GO_AWAY)
        self.assertEqual(self.transport.value(), b'c[3000,"Go away!"]')

    def test_writeData(self):
        '''writeData writes the provided data to the transport.'''
        self.protocol.writeData(["letter", 2])
        self.assertEqual(self.transport.value(), b'a["letter",2]')

    def test_dataReceived(self):
        '''The wrapped protocol receives deserialized JSON data.'''
        self.protocol.dataReceived(b'["letter",2]')
        self.protocol.dataReceived(b'["another",null]')
        self.assertEqual(self.receivedData, [["letter", 2],
                                             ["another", None]])

    def test_closeFrame(self):
        '''closeFrame returns a serialized close frame for use by the
        caller.

        '''
        frame = self.protocol.closeFrame(P.DISCONNECT.GO_AWAY)
        self.assertEqual(frame, b'c[3000,"Go away!"]')
        self.assertFalse(self.transport.value())

    def test_emptyDataReceived(self):
        '''The wrapped protocol does not receive empty strings and the sender
        receives an error message.

        '''
        with self.assertRaises(P.InvalidData) as excContext:
            self.protocol.dataReceived(b'')

        self.assertEqual(excContext.exception.reason,
                         P.INVALID_DATA.NO_PAYLOAD.value)
        self.assertFalse(self.receivedData)

    def test_badJSONReceived(self):
        '''The wrapped protocol does not receive malformed JSON and the sender
        receives an error message.

        '''
        with self.assertRaises(P.InvalidData) as excContext:
            self.protocol.dataReceived(b'!!!')

        self.assertEqual(excContext.exception.reason,
                         P.INVALID_DATA.BAD_JSON.value)
        self.assertFalse(self.receivedData)

    def test_jsonEncoder(self):
        '''SockJSWireProtocolWrapper can use a json.JSONEncoder subclass for
        writes.

        '''
        class ComplexEncoder(json.JSONEncoder):
            def default(self, obj):
                if isinstance(obj, complex):
                    return [obj.real, obj.imag]
                # Let the base class default method raise the TypeError
                return json.JSONEncoder.default(self, obj)

        factory = P.SockJSWireProtocolWrappingFactory(
            self.wrappedFactory,
            jsonEncoder=ComplexEncoder)

        encodingProtocol = factory.buildProtocol(self.address)
        encodingProtocol.makeConnection(self.transport)

        encodingProtocol.writeData([2 + 1j])

        self.assertEqual(self.transport.value(), b'a[[2.0,1.0]]')

    def test_jsonDecoder(self):
        '''SockJSWireProtocolWrapper can use a json.JSONDecoder subclass for
        receives.

        '''
        class SetDecoder(json.JSONDecoder):
            def __init__(self, *args, **kwargs):
                kwargs['object_hook'] = self.set_object_hook
                super(SetDecoder, self).__init__(*args, **kwargs)

            def set_object_hook(self, obj):
                if isinstance(obj, dict) and obj.get('!set'):
                    return set(obj['!set'])
                return obj

        factory = P.SockJSWireProtocolWrappingFactory(
            self.wrappedFactory,
            jsonDecoder=SetDecoder)

        encodingProtocol = factory.buildProtocol(self.address)
        encodingProtocol.makeConnection(self.transport)

        encodingProtocol.dataReceived(b'{"!set": [1, 2, 3]}')
        self.assertEqual(self.receivedData, [{1, 2, 3}])


class RecordsWireProtocolActions(object):

    def __init__(self):
        self.lostConnection = 0
        self.wroteOpen = 0
        self.wroteHeartbeat = 0
        self.wroteData = []
        self.wroteClose = []

    def empty(self):
        return not any([self.lostConnection,
                        self.wroteOpen,
                        self.wroteHeartbeat,
                        self.wroteData,
                        self.wroteClose])


class FakeSockJSWireProtocol(object):

    def __init__(self, recorder):
        self._recorder = recorder

    def loseConnection(self):
        self._recorder.lostConnection += 1

    def writeOpen(self):
        self._recorder.wroteOpen += 1

    def writeHeartbeat(self):
        self._recorder.wroteHeartbeat += 1

    def writeData(self, data):
        self._recorder.wroteData.append(data)

    def writeClose(self, reason):
        self._recorder.wroteClose.append(reason)


class RecordsHeartbeat(object):

    def __init__(self):
        self.scheduleCalls = 0
        self.stopCalls = 0

    def scheduleCalled(self):
        self.scheduleCalls += 1

    def stopCalled(self):
        self.stopCalls += 1


class FakeHeartbeatClock(object):

    def __init__(self, recorder):
        self.writeHeartbeat = None
        self._recorder = recorder

    def schedule(self):
        self._recorder.scheduleCalled()

    def stop(self):
        self._recorder.stopCalled()


class SockJSProtocolMachineTestCase(unittest.TestCase):

    def setUp(self):
        self.heartbeatRecorder = RecordsHeartbeat()
        self.heartbeater = FakeHeartbeatClock(self.heartbeatRecorder)
        self.sockJSMachine = P.SockJSProtocolMachine(self.heartbeater)
        self.protocolRecorder = RecordsWireProtocolActions()
        self.sockJSWireProtocol = FakeSockJSWireProtocol(self.protocolRecorder)

    def test_disconnectBeforeConnect(self):
        '''Disconnecting before connecting permanently disconnects
        a SockJSProtocolMachine.

        '''
        self.sockJSMachine.disconnect()
        self.assertTrue(self.protocolRecorder.empty())

        with self.assertRaises((KeyError, automat.NoTransition)):
            self.sockJSMachine.connect(self.sockJSWireProtocol)

    def test_connect(self):
        '''SockJSProtocolMachine.connect writes an opening frame and schedules
        a heartbeat.

        '''
        self.sockJSMachine.connect(self.sockJSWireProtocol)
        self.assertEqual(self.protocolRecorder.wroteOpen, 1)
        self.assertEqual(self.heartbeatRecorder.scheduleCalls, 1)

    def test_write(self):
        '''SockJSProtocolMachine.write writes the requested data and
        (re)schedules a heartbeat.

        '''
        self.sockJSMachine.connect(self.sockJSWireProtocol)
        self.sockJSMachine.write([1, 'something'])

        self.assertEqual(self.protocolRecorder.wroteOpen, 1)
        self.assertEqual(self.protocolRecorder.wroteData, [[1, 'something']])
        self.assertEqual(self.heartbeatRecorder.scheduleCalls, 2)

    def test_heartbeat(self):
        '''SockJSProtocolMachine.heartbeat writes a heartbeat!'''
        self.sockJSMachine.connect(self.sockJSWireProtocol)
        self.sockJSMachine.heartbeat()

        self.assertEqual(self.protocolRecorder.wroteOpen, 1)
        self.assertEqual(self.protocolRecorder.wroteHeartbeat, 1)
        self.assertEqual(self.heartbeatRecorder.scheduleCalls, 1)

    def test_withHeartBeater(self):
        '''SockJSProtocolMachine.withHeartbeater should associate a new
        instance's heartbeat method with the heartbeater.

        '''
        instance = P.SockJSProtocolMachine.withHeartbeater(
            self.heartbeater)
        instance.connect(self.sockJSWireProtocol)
        self.heartbeater.writeHeartbeat()

        self.assertEqual(self.protocolRecorder.wroteOpen, 1)
        self.assertEqual(self.protocolRecorder.wroteHeartbeat, 1)
        self.assertEqual(self.heartbeatRecorder.scheduleCalls, 1)

    def test_receive(self):
        '''SockJSProtocolMachine.receive passes decoded data through.'''
        self.sockJSMachine.connect(self.sockJSWireProtocol)
        data = [1, 'something']
        self.assertEqual(self.sockJSMachine.receive(data), data)

    def test_disconnect(self):
        '''SockJSProtocolMachine.disconnect implements an active close: it
        writes a close frame, disconnects the transport, and cancels
        any pending heartbeats.

        '''
        self.sockJSMachine.connect(self.sockJSWireProtocol)
        self.sockJSMachine.disconnect(reason=P.DISCONNECT.GO_AWAY)

        self.assertIs(None, self.sockJSMachine.transport)

        self.assertEqual(self.protocolRecorder.wroteOpen, 1)
        self.assertEqual(self.protocolRecorder.wroteClose,
                         [P.DISCONNECT.GO_AWAY])
        self.assertEqual(self.protocolRecorder.lostConnection, 1)

        self.assertEqual(self.heartbeatRecorder.stopCalls, 1)

    def test_close(self):
        '''SockJSProtocolMachine.close implements a passive close: it drops
        the transport and cancels any pending heartbeats.

        '''
        self.sockJSMachine.connect(self.sockJSWireProtocol)
        self.sockJSMachine.close()

        self.assertIs(None, self.sockJSMachine.transport)
        self.assertEqual(self.protocolRecorder.wroteOpen, 1)
        self.assertEqual(self.heartbeatRecorder.stopCalls, 1)


class RecordsProtocolMachineActions(object):

    def __init__(self):
        self.connect = []
        self.received = []
        self.written = []
        self.disconnected = 0
        self.closed = 0


class FakeSockJSProtocolMachine(object):

    def __init__(self, recorder):
        self._recorder = recorder

    def connect(self, transport):
        self._recorder.connect.append(transport)

    def receive(self, data):
        self._recorder.received.append(data)
        return data

    def write(self, data):
        self._recorder.written.append(data)

    def disconnect(self):
        self._recorder.disconnected += 1

    def close(self):
        self._recorder.closed += 1


class SockJSProtocolTestCase(unittest.TestCase):

    def setUp(self):
        self.connectionLost = []
        self.stateMachineRecorder = RecordsProtocolMachineActions()

        wrappedFactory = EchoProtocolFactory(self.connectionLost)
        self.factory = P.SockJSProtocolFactory(wrappedFactory)

        def fakeStateMachineFactory():
            return FakeSockJSProtocolMachine(self.stateMachineRecorder)

        self.factory.stateMachineFactory = fakeStateMachineFactory

        self.address = IPv4Address('TCP', '127.0.0.1', 80)
        self.transport = StringTransport()

        self.protocol = self.factory.buildProtocol(self.address)
        self.protocol.makeConnection(self.transport)

    def test_makeConnection(self):
        '''makeConnection connects the state machine to the transport.'''
        self.assertEqual(self.stateMachineRecorder.connect, [self.transport])

    def test_dataReceived_write(self):
        '''dataReceived passes the data to the state machine's receive method
        and the wrapped protocol.  With our echo protocol, we also
        test that write() calls the machine's write method.

        '''
        self.protocol.dataReceived(b'"something"')
        self.assertEqual(self.stateMachineRecorder.received,
                         [b'"something"'])
        self.assertEqual(self.stateMachineRecorder.written,
                         [b'"something"'])

    def test_dataReceived_writeSequence(self):
        '''dataReceived passes the data to the state machine's receive method
        and the wrapped protocol.  With our echo protocol, we also
        test that writeSequence() calls the machine's write method.

        '''
        self.protocol.dataReceived([b'"x"', b'"y"'])
        self.assertEqual(self.stateMachineRecorder.received,
                         [[b'"x"', b'"y"']])
        # multiple write calls
        self.assertEqual(self.stateMachineRecorder.written,
                         [b'"x"', b'"y"'])

    def test_loseConnection(self):
        '''loseConnection calls the state machine's disconnect method.'''
        self.protocol.loseConnection()
        self.assertEqual(self.stateMachineRecorder.disconnected, 1)

    def test_connectionLost(self):
        '''connectionLost calls the state machine's close method and the
        wrapped protocol's connectionLost method.

        '''
        reason = "This isn't a real reason"
        self.protocol.connectionLost(reason)
        self.assertEqual(self.stateMachineRecorder.closed, 1)
        self.assertEqual(self.connectionLost, [reason])


class RecordsRequestSessionActions(object):

    def __init__(self):
        self.request = None
        self.connectionsEstablished = []
        self.connectionsCompleted = 0
        self.requestsBegun = 0
        # TODO - these next two are needlessly confusing -- rename one
        # or both!
        self.receivedData = []
        self.dataReceived = []

        self.completelyWritten = []
        self.otherRequestsClosed = []
        self.dataWritten = []
        self.heartbeatsCompleted = 0
        self.currentRequestsFinished = 0
        self.connectionsLostCompletely = 0
        self.connectionsCompletelyLost = []
        self.connectionsMadeFromRequest = []


class FakeRequestSessionProtocolWrapper(object):

    def __init__(self, recorder):
        self.recorder = recorder
        self.terminationDeferred = Deferred()

    @property
    def request(self):
        return self.recorder.request

    @request.setter
    def request(self, request):
        self.recorder.request = request

    def makeConnectionFromRequest(self, request):
        self.recorder.connectionsMadeFromRequest.append(request)

    def establishConnection(self, request):
        self.recorder.connectionsEstablished.append(request)

    def completeConnection(self):
        self.recorder.connectionsCompleted += 1

    def beginRequest(self):
        self.recorder.requestsBegun += 1

    def completeDataReceived(self, data):
        self.recorder.dataReceived.append(data)

    def closeOtherRequest(self, request, reason):
        self.recorder.otherRequestsClosed.append((request, reason))

    def dataReceived(self, data):
        self.recorder.receivedData.append(data)

    def writeData(self, data):
        self.recorder.dataWritten.append(data)

    def completeWrite(self, data):
        self.recorder.completelyWritten.append(data)

    def completeHeartbeat(self):
        self.recorder.heartbeatsCompleted += 1

    def finishCurrentRequest(self):
        self.recorder.currentRequestsFinished += 1
        self.request = None

    def closeFrame(self, reason):
        return reason

    def completeLoseConnection(self):
        self.recorder.connectionsLostCompletely += 1

    def completeConnectionLost(self, reason):
        self.recorder.connectionsCompletelyLost.append(reason)


class DummyRequestAllowsNonBytes(DummyRequest):
    '''A DummyRequest subclass that does not assert write has been called
    with bytes.  Use me when you want to inspect something that an
    intermediary would have serialized before writing it to the
    request.

    '''

    def write(self, data):
        self.written.append(data)


class RequestSessionMachineTestCase(unittest.TestCase):

    def setUp(self):
        self.recorder = RecordsRequestSessionActions()
        self.fakeRequestSession = FakeRequestSessionProtocolWrapper(
            self.recorder)
        self.requestSessionMachine = P.RequestSessionMachine(
            self.fakeRequestSession)

        self.request = DummyRequest([b'ignored'])

    def test_firstAttach(self):
        '''Attaching the first request to a RequestSessionMachine sets up the
        the protocol wrapper, begins the request, then attaches the
        protocol wrapper to the wrapped protocol as its transport.

        '''
        self.requestSessionMachine.attach(self.request)
        self.assertIs(self.recorder.request, self.request)
        self.assertEqual(self.recorder.connectionsEstablished, [self.request])
        self.assertEqual(self.recorder.requestsBegun, 1)
        self.assertEqual(self.recorder.connectionsCompleted, 1)

    def test_connectedHaveTransportWrite(self):
        '''With an attached request, write calls completeWrite and does not
        buffer.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.write(b"abc")
        self.assertEqual(self.recorder.completelyWritten, [b"abc"])

    def test_connectedHaveTransportReceive(self):
        '''With an attached request, received data passes on to the wrapped
        protocol.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.receive(b"abc")
        self.assertEqual(self.recorder.dataReceived, [b"abc"])

    def test_connectedHaveTransportHeartbeat(self):
        '''With an attached request, heartbeats are immediately sent.'''
        self.test_firstAttach()
        self.requestSessionMachine.heartbeat()
        self.assertEqual(self.recorder.heartbeatsCompleted, 1)

    def test_connectedHaveTransportDetach(self):
        '''Detaching from a request finishes that request.'''
        self.test_firstAttach()
        self.requestSessionMachine.detach()
        self.assertEqual(self.recorder.currentRequestsFinished, 1)

    def assertDuplicateRequestClosedWith(self, reason):
        '''Assert that a second request is finished with the given reason.'''

        duplicateRequest = 'not really a request'
        self.requestSessionMachine.attach(duplicateRequest)
        self.assertEqual(self.recorder.otherRequestsClosed,
                         [(duplicateRequest, reason)])

    def test_connectedHaveTransportDuplicateAttach(self):
        '''Attempting to attach a request to a RequestSessionMachine that's
        already attached to a request closes the attached request.

        '''
        self.test_firstAttach()
        self.assertDuplicateRequestClosedWith(P.DISCONNECT.STILL_OPEN)
        duplicateRequest = 'not really a request'
        self.requestSessionMachine.attach(duplicateRequest)

    def test_connectedHaveTransportWriteCloseAndLoseConnection(self):
        '''Writing a close frame to a RequestSessionMachine stores it on the
        machine so it will be written upon loseConnection.


        '''
        self.test_firstAttach()
        self.requestSessionMachine.writeClose(P.DISCONNECT.GO_AWAY)
        self.requestSessionMachine.loseConnection()

        self.assertDuplicateRequestClosedWith(P.DISCONNECT.GO_AWAY)
        self.assertEqual(self.recorder.connectionsLostCompletely, 1)

    def test_connectedHaveTransportLoseConnection(self):
        '''Losing the connection closes the connection and closes the
        wrapped protocol.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.loseConnection()
        self.assertEqual(self.recorder.connectionsLostCompletely, 1)

    def test_connectedHaveTransportConnectionLost(self):
        '''connectionLost unsets the RequestSession's request (but does *not*
        call its finish() a second time) and calls the wrapped
        protocol's connectionLost.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.connectionLost(reason="Some Reason")
        self.assertIsNone(self.recorder.request)
        self.assertEqual(self.recorder.connectionsCompletelyLost,
                         ["Some Reason"])
        self.assertFalse(self.request.finished)

    def test_connectedNoTransportEmptyBufferReceive(self):
        '''The wrapped protocol receives data even when there's no attached
        outgoing request.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()

        deserializedMessage = ["I wasn't serialized'"]

        self.requestSessionMachine.receive(deserializedMessage)

    def test_connectedNoTransportEmptyBufferHeartbeat(self):
        '''Heartbeats are not sent when there's no attached request and the
        write buffer is empty.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()

        self.requestSessionMachine.heartbeat()
        self.assertEqual(self.recorder.heartbeatsCompleted, 0)

    def test_connectedNoTransportEmptyBufferDetach(self):
        '''Detaching a RequestSessionMachine that's already detached is a safe
        noop, so wrappers can always call detach() safel.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()
        self.requestSessionMachine.detach()

    def test_connectedNoTransportEmptyBufferWriteCloseAndLoseConnection(self):
        '''Writing a close frame to a RequestSessionMachine stores it on the
        machine so it will be written upon loseConnection.


        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()
        self.requestSessionMachine.writeClose(P.DISCONNECT.GO_AWAY)
        self.requestSessionMachine.loseConnection()

        self.assertDuplicateRequestClosedWith(P.DISCONNECT.GO_AWAY)
        self.assertEqual(self.recorder.connectionsLostCompletely, 1)

    def test_connectedNoTransportEmptyBufferConnectionLost(self):
        '''A RequestSessionMachine with no attached request and an empty
        buffer simply closes the protocol upon connectionLost.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()

        someReason = 'not a real reason'

        self.requestSessionMachine.connectionLost(someReason)
        self.assertEqual(self.recorder.connectionsCompletelyLost, [someReason])

    def test_noTransportWriteThenAttach(self):
        '''Writes are buffered when there's no attached request.  Attaching a
        request flushes the buffer.
        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()

        unserializedMessage = ["I wasn't serialized"]

        self.requestSessionMachine.write(unserializedMessage)
        self.requestSessionMachine.write(unserializedMessage)
        self.assertEqual(self.requestSessionMachine.buffer,
                         unserializedMessage * 2)

        newRequest = DummyRequestAllowsNonBytes([b'newRequest'])

        self.requestSessionMachine.attach(newRequest)
        self.assertEqual(self.requestSessionMachine.buffer, [])

        # the two lists have been concatenated into one, and were
        # flushed with a single call to requestSession.writeData
        self.assertEqual(self.recorder.dataWritten,
                         [unserializedMessage * 2])

    def test_connectedNoTransportPendingReceive(self):
        '''Received data passes immediately to the wrapped protocol, even when
        there's pending data.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()
        self.requestSessionMachine.write(b'abc')
        self.requestSessionMachine.receive(b'xyz')
        self.assertEqual(self.recorder.dataReceived, [b'xyz'])

    def test_connectedNoTransportPendingHeartbeat(self):
        '''Heartbeats are not sent when there's no attached request and
        pending data.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()
        self.requestSessionMachine.write(b'abc')
        self.requestSessionMachine.heartbeat()
        self.assertEqual(self.recorder.heartbeatsCompleted, 0)

    def test_connectedNoTransportPendingWriteCloseAndLoseConnection(self):
        '''Writing a close frame to a RequestSessionMachine stores it on the
        machine so it will be written upon loseConnection.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()
        self.requestSessionMachine.write(b'abc')
        self.requestSessionMachine.writeClose(P.DISCONNECT.GO_AWAY)
        self.requestSessionMachine.loseConnection()

        self.assertDuplicateRequestClosedWith(P.DISCONNECT.GO_AWAY)
        self.assertEqual(self.recorder.connectionsLostCompletely, 1)

    def test_connectedNoTransportPendingConnectionLost(self):
        '''If the session times out before all data can be written,
        connectionLost provides calls the wrapped protocol's ConnectionLost
        with a SessionTimeout failure.

        '''
        self.test_firstAttach()
        self.requestSessionMachine.detach()
        self.requestSessionMachine.write('xyz')
        self.requestSessionMachine.connectionLost()
        self.assertEqual(len(self.recorder.connectionsCompletelyLost), 1)
        failure = self.recorder.connectionsCompletelyLost[0]
        failure.trap(P.SessionTimeout)


class TimeoutClockTestCase(unittest.TestCase):

    def setUp(self):
        self.timeoutDeferred = Deferred()
        self.clock = Clock()
        self.length = 5.0

        self.timeoutClock = P.TimeoutClock(self.timeoutDeferred,
                                           length=self.length,
                                           clock=self.clock)

    def test_start(self):
        '''A timeout expires a connection if not interrupted and then
        stops.

        '''

        self.timeoutClock.start()

        pendingExpiration = self.timeoutClock.timeoutCall
        self.assertEqual(self.clock.getDelayedCalls(), [pendingExpiration])

        self.clock.advance(self.length * 2)

        def assertTimedOutAndCannotRestartOrStop(_):
            self.assertFalse(self.clock.getDelayedCalls())
            self.assertIsNone(self.timeoutClock.timeoutCall)

            with self.assertRaises(RuntimeError):
                self.timeoutClock.start()

            with self.assertRaises(RuntimeError):
                self.timeoutClock.reset()

            # no effect
            self.timeoutClock.stop()
            self.assertFalse(self.clock.getDelayedCalls())
            self.assertIsNone(self.timeoutClock.timeoutCall)

        self.timeoutDeferred.addCallback(assertTimedOutAndCannotRestartOrStop)
        return self.timeoutDeferred

    def test_reset_interrupts(self):
        '''A reset() call will remove the pending timeout, so that a
        subsequent start() call reschedule it.

        '''
        self.timeoutClock.start()

        pendingExpiration = self.timeoutClock.timeoutCall
        self.assertEqual(self.clock.getDelayedCalls(), [pendingExpiration])

        self.timeoutClock.reset()

        resetPendingExecution = self.timeoutClock.timeoutCall
        self.assertIsNot(pendingExpiration, resetPendingExecution)
        self.assertEqual(self.clock.getDelayedCalls(), [])

    def test_stop(self):
        '''A stop() call to the stops the pending timeout and is idempotent.
        '''
        self.timeoutClock.start()
        pendingExpiration = self.timeoutClock.timeoutCall
        self.assertEqual(self.clock.getDelayedCalls(), [pendingExpiration])

        self.timeoutClock.stop()
        self.assertEqual(self.clock.getDelayedCalls(), [])
        self.assertIsNone(self.timeoutClock.timeoutCall)

        self.timeoutClock.stop()
        self.assertEqual(self.clock.getDelayedCalls(), [])
        self.assertIsNone(self.timeoutClock.timeoutCall)


class RecordsTimeoutClockActions(object):
    startCalls = 0
    stopCalls = 0
    resetCalls = 0


class FakeTimeoutClock(object):

    def __init__(self, recorder):
        self.recorder = recorder

    def start(self):
        self.recorder.startCalls += 1

    def stop(self):
        self.recorder.stopCalls += 1

    def reset(self):
        self.recorder.resetCalls += 1


class RecordsSessionMachineActions(object):

    def __init__(self):
        self.attachedRequests = []
        self.detachCalls = 0
        self.dataWritten = []
        self.receivedData = []
        self.closeReasonsWritten = []
        self.heartbeatCalls = 0
        self.loseConnectionCalls = 0
        self.connectionsLostReasons = []


class FakeRequestSessionMachine(object):

    def __init__(self, recorder):
        self.recorder = recorder

    def attach(self, request):
        self.recorder.attachedRequests.append(request)

    def detach(self):
        self.recorder.detachCalls += 1

    def write(self, data):
        self.recorder.dataWritten.append(data)

    def receive(self, data):
        self.recorder.receivedData.append(data)

    def writeClose(self, reason):
        self.recorder.closeReasonsWritten.append(reason)

    def heartbeat(self):
        self.recorder.heartbeatCalls += 1

    def loseConnection(self):
        self.recorder.loseConnectionCalls += 1

    def connectionLost(self, reason):
        self.recorder.connectionsLostReasons.append(reason)


class RequestSessionProtocolWrapperTestCase(unittest.TestCase):
    '''Tests for the ProtocolWrapper that adapts a
    twisted.web.server.Request to a SockJS polling transport.

    '''

    def setUp(self):
        self.receivedData = []
        self.connectionsLost = []

        self.timeoutClockRecorder = RecordsTimeoutClockActions()
        self.timeoutClock = FakeTimeoutClock(self.timeoutClockRecorder)

        self.sessionMachineRecorder = RecordsSessionMachineActions()
        self.sessionMachine = FakeRequestSessionMachine(
            self.sessionMachineRecorder)

        self.wrappedFactory = RecordingProtocolFactory(self.receivedData,
                                                       self.connectionsLost)
        # TODO: is it better to test this with SockJSProtocol?  Right
        # now it seems the answer is no, because it's better to test
        # the protocol wrapping functionality against the generic
        # interface.
        self.factory = self.makeFactory()
        self.factory.timeoutClockFactory = self.fakeTimeoutClockFactory
        self.factory.sessionMachineFactory = self.fakeSessionMachineFactory

        self.address = IPv4Address('TCP', '127.0.0.1', 80)
        self.protocol = self.factory.buildProtocol(self.address)
        self.request = DummyRequest([b'ignored'])

    def makeFactory(self):
        '''Returns the WrappingFactory for this test case.  Override me in
        subclasses that test different session wrapper protocols.

        '''
        return P.RequestSessionWrappingFactory(self.wrappedFactory)

    def fakeTimeoutClockFactory(self, terminationDeferred):
        return self.timeoutClock

    def fakeSessionMachineFactory(self, protocol):
        return self.sessionMachine

    def test_makeConnection_fails(self):
        '''You can't call makeConnection on a
        RequestSessionProtocolWrapper.

        '''
        with self.assertRaises(RuntimeError):
            self.protocol.makeConnection('ignored')

    def test_attached(self):
        '''The attached property returns True iff a request is attached.'''

        self.assertFalse(self.protocol.request)
        self.assertFalse(self.protocol.attached)
        self.protocol.request = self.request
        self.assertTrue(self.protocol.attached)

    def test_makeConnectionFromRequest(self):
        '''makeConnectionFromRequest has the session state machine attach the
        request.

        '''
        self.protocol.makeConnectionFromRequest(self.request)
        self.assertEqual(self.sessionMachineRecorder.attachedRequests,
                         [self.request])

    def test_detachFromRequest(self):
        '''detachFromRequest has the session state machine perform the detach.

        '''
        self.protocol.detachFromRequest()
        self.assertEqual(self.sessionMachineRecorder.detachCalls, 1)

    def test_write(self):
        '''write adds a newline before writing the data to the current
        request.

        '''
        self.protocol.request = self.request
        self.protocol.write(b'something')
        self.assertEqual(self.request.written, [b'something\n'])

    def test_closeOtherRequest(self):
        '''closeOtherRequest writes a close frame consisting of a reason and a
        newline to a request.

        '''
        self.protocol.closeOtherRequest(self.request, P.DISCONNECT.GO_AWAY)
        self.assertEqual(self.request.written, [b'c[3000,"Go away!"]\n'])

    def test_dataReceived(self):
        '''dataReceived passes the data off to the session state machine.

        '''
        self.protocol.dataReceived('something')
        self.assertEqual(self.sessionMachineRecorder.receivedData,
                         ['something'])

    def test_writeData(self):
        '''writeData passes the data off to the session state machine.'''
        self.protocol.writeData('something')
        self.assertEqual(self.sessionMachineRecorder.dataWritten,
                         ['something'])

    def test_writeHeartbeat(self):
        '''writeHeartbeat has the session state machine write a heartbeat.'''
        self.protocol.writeHeartbeat()
        self.assertEqual(self.sessionMachineRecorder.heartbeatCalls, 1)

    def test_writeClose(self):
        '''writeClose has the session state machine close the request.'''
        self.protocol.writeClose("reason")
        self.assertEqual(self.sessionMachineRecorder.closeReasonsWritten,
                         ["reason"])

    def test_loseConnection(self):
        '''loseConnection tells the session state machine to lose the
        connection and the timeout clock to start, but does both only once.

        '''
        self.protocol.loseConnection()
        self.assertTrue(self.protocol.disconnecting)
        self.assertEqual(self.sessionMachineRecorder.loseConnectionCalls, 1)
        self.assertEqual(self.timeoutClockRecorder.startCalls, 1)

        self.protocol.loseConnection()
        self.assertTrue(self.protocol.disconnecting)
        self.assertEqual(self.sessionMachineRecorder.loseConnectionCalls, 1)
        self.assertEqual(self.timeoutClockRecorder.startCalls, 1)

    def test_connectionLost_disconnecting(self):
        '''If connectionLost has been called after loseConnection, then this
        connection will linger in a disconnected state until the
        timeout expires.  The protocol's terminationDeferred does not
        fire and the timeout clock is not stopped, but the session
        machine learns about the lost connection.

        '''
        self.protocol.disconnecting = 1
        self.protocol.connectionLost("reason")
        unfiredDeferred = self.protocol.terminationDeferred
        with self.assertRaises(AttributeError):
            unfiredDeferred.result

        self.assertFalse(self.timeoutClockRecorder.stopCalls)
        self.assertEqual(self.sessionMachineRecorder.connectionsLostReasons,
                         ['reason'])
        self.assertIsNone(self.protocol.sessionMachine)

    def test_connectionLost_clientClose(self):
        '''If connectionLost is called because the client closed the
        connection, then this connection has disappeared suddenly.
        Consequently, the protocol's terminationDeferred errbacks with
        the provided reason, the timeout clock is stopped, and the
        session machine learns about the lost connection.

        '''
        erroredDeferred = self.protocol.terminationDeferred

        def trapConnectionDone(failure):
            failure.trap(error.ConnectionDone)

        erroredDeferred.addErrback(trapConnectionDone)

        self.protocol.connectionLost(connectionDone)

        self.assertEqual(self.timeoutClockRecorder.stopCalls, 1)
        self.assertEqual(self.sessionMachineRecorder.connectionsLostReasons,
                         [connectionDone])
        self.assertIsNone(self.protocol.sessionMachine)

        return erroredDeferred

    def test_consumerProducer_notImplemented(self):
        '''Registration of consumers and producers is not implemented.'''
        with self.assertRaises(NotImplementedError):
            self.protocol.registerProducer(None, None)

        with self.assertRaises(NotImplementedError):
            self.protocol.unregisterProducer()

    def test_beginRequest_timeout_reset(self):
        '''Beginning a request resets the timeout.

        '''
        self.protocol.request = self.request
        self.protocol.beginRequest()
        self.assertTrue(self.timeoutClockRecorder.resetCalls, 1)

    def test_beginRequest_finishedNotifier_forwards_failures(self):
        '''Beginning a request retrieves a Deferred from that request that
        forwards failures to the protocol's connectionLost.

        '''
        self.protocol.request = self.request
        self.protocol.beginRequest()

        reason = connectionDone

        def assertConnectionLostCalled(ignored):
            recordedExceptions = [
                reason.value for reason in
                self.sessionMachineRecorder.connectionsLostReasons]
            self.assertEqual(recordedExceptions, [reason.value])

        finishedNotifier = self.protocol.finishedNotifier
        finishedNotifier.addCallback(assertConnectionLostCalled)

        def trapConnectionDone(failure):
            failure.trap(error.ConnectionDone)

        terminationDeferred = self.protocol.terminationDeferred
        terminationDeferred.addErrback(trapConnectionDone)

        self.request.processingFailed(reason)
        return DeferredList([finishedNotifier, terminationDeferred])

    def test_beginRequest_finishedNotifier_traps_cancellation(self):
        '''Beginning a request retrieves a Deferred from the request that
        traps cancellation errors, preventing them from reaching the
        protocol's connectionLost.

        '''
        self.protocol.request = self.request
        self.protocol.beginRequest()
        finishedNotifier = self.protocol.finishedNotifier

        def assertConnectionLostNotCalled(ignored):
            self.assertEqual(
                self.sessionMachineRecorder.connectionsLostReasons,
                [])

        finishedNotifier.addCallback(assertConnectionLostNotCalled)

        finishedNotifier.cancel()
        return finishedNotifier

    def test_establishConnection(self):
        '''Establishing a connection makes the RequestSessionProtocolWrapper
        instance directly provide the same interface as the request's
        transport, but does *not* call makeConnection, and thus
        connectionMade, on the wrapped protocol.  That's because we may
        decide to immediately close the request as part of the polling
        transport handshake.  This lets us interpose state changes
        that set up buffering between the handshake and the protocol's
        connectionMade logic.

        '''
        class IStubTransport(Interface):
            pass

        @implementer(IStubTransport)
        class StubTransport:
            pass

        # Looking up what RequestSessionProtocolWrapper implements
        # also mutates the class.  It adds __implemented__ and
        # __providedBy__ attributes to it.  These prevent __getattr__
        # from causing the IStubTransport.providedBy call below from
        # returning True.  If, by accident, nothing else causes these
        # attributes to be added to ProtocolWrapper, the test will
        # pass, but the interface will only be provided until
        # something does trigger their addition.  So we just trigger
        # it right now to be sure.
        implementedBy(P.RequestSessionProtocolWrapper)

        self.request.transport = StubTransport()
        self.protocol.establishConnection(self.request)
        self.assertTrue(IStubTransport.providedBy(self.protocol))
        self.assertFalse(self.protocol.wrappedProtocol.connectionMadeCalls)

    def test_completeConnection(self):
        '''Completing a connection attaches the RequestSessionProtocolWrapper
        instance to the wrapped protocol as the wrapped protocol's
        transport and completes the Protocol's connection.

        '''
        self.protocol.completeConnection()
        self.assertIs(self.protocol.wrappedProtocol.transport, self.protocol)
        self.assertEqual(self.protocol.wrappedProtocol.connectionMadeCalls,
                         1)

    def test_completeDataReceived(self):
        '''Completing data reception passes that data on to the wrapped
        protocol.

        '''
        self.protocol.completeDataReceived(b'["a"]')
        self.assertEqual(self.receivedData, [["a"]])

    def test_completeWrite(self):
        '''Completing a write serializes the data to the request.'''
        self.protocol.request = self.request
        self.protocol.completeWrite(["a"])
        self.assertEqual(self.request.written, [b'a["a"]\n'])

    def test_completeHeartbeat(self):
        '''Completing a write serializes the data to the request.'''
        self.protocol.request = self.request
        self.protocol.completeHeartbeat()
        self.assertEqual(self.request.written, [b'h\n'])

    def test_completeConnectionLost(self):
        '''Completing a lost connection calls the wrapped protocol's
        connectionLost.

        '''
        self.request.transport = StringTransport()
        self.protocol.establishConnection(self.request)
        self.protocol.completeConnectionLost(connectionDone)
        self.assertEqual(self.connectionsLost, [connectionDone])

    def test_completeLoseConnection(self):
        '''Completing losing a connection calls the wrapped protocol's
        loseConnection.

        '''
        self.protocol.transport = transport = StringTransport()
        self.protocol.completeLoseConnection()
        self.assertTrue(transport.disconnecting)

    def test_finishCurrentRequest(self):
        '''Finishing the current request fires the finishedNotifer, calls
        finish on the request, unsets the protocol's request and
        finishedNotifier, and starts the timeout clock.

        '''
        self.protocol.request = self.request
        self.protocol.beginRequest()

        finishedNotifier = self.protocol.finishedNotifier

        self.protocol.finishCurrentRequest()

        self.assertGreater(self.request.finished, 0)
        self.assertFalse(self.protocol.attached)
        self.assertIsNone(self.protocol.finishedNotifier)
        self.assertEqual(self.timeoutClockRecorder.startCalls, 1)
        return finishedNotifier

    def test_timedOutCallback(self):
        '''The termination deferred's callback sets disconnecting and calls
        connectionLost.  Setting disconnecting avoids errbacking the
        deferred that's just been fired!

        '''
        terminationDeferred = self.protocol.terminationDeferred

        def assertConnectionLostCalled(ignored):
            self.assertTrue(self.protocol.disconnecting)
            self.assertEqual(self.sessionMachineRecorder.connectionsLost,
                             connectionDone)

        terminationDeferred.callback(P.TimeoutClock.EXPIRED)
        return terminationDeferred


class SessionHouseTestCase(unittest.TestCase):

    def setUp(self):
        self.sessions = P.SessionHouse()
        self.sessionID = b'session'
        self.request = DummyRequest([b'server', self.sessionID, b'ignored'])
        self.request.transport = StringTransport()

        self.recorder = RecordsRequestSessionActions()
        self.protocol = FakeRequestSessionProtocolWrapper(self.recorder)

    def buildProtocol(self, address):
        return self.protocol

    def test_validateAndExtraSessionID(self):
        '''Invalid server or session IDs result in None, while valid ones
        result in a sessionID.

        '''
        noIDs = DummyRequest([])
        self.assertIsNone(self.sessions.validateAndExtractSessionID(noIDs))

        emptyIDs = DummyRequest([b'', b'', b''])
        self.assertIsNone(self.sessions.validateAndExtractSessionID(emptyIDs))

        hasDot = DummyRequest([b'server', b'session', b'has.thatdot'])
        self.assertIsNone(self.sessions.validateAndExtractSessionID(hasDot))

        self.assertEqual(
            self.sessions.validateAndExtractSessionID(self.request),
            b'session')

    def test_attachToSession_returns_False(self):
        '''attachToSession returns False if a request with invalid IDs
        attempts to attaches to a session.

        '''
        self.assertFalse(self.sessions.attachToSession(self, DummyRequest([])))

    def test_attachToSession_new_session(self):
        '''attachToSession creates a new session when given a request with a
        novel and valid session ID.

        '''
        self.assertTrue(self.sessions.attachToSession(self, self.request))
        self.assertIs(self.sessions.sessions[self.sessionID], self.protocol)
        self.assertEqual(self.recorder.connectionsMadeFromRequest,
                         [self.request])

    def test_sessionClosed_on_callback(self):
        '''Firing the protocol's terminationDeferred removes the session from
        the house.

        '''
        self.test_attachToSession_new_session()
        self.protocol.terminationDeferred.callback(None)
        self.assertNotIn(self.sessionID, self.sessions.sessions)
        return self.protocol.terminationDeferred

    def test_sessionClosed_on_errback(self):
        '''Errbacking the protocol's terminationDeferred removes the session
        from the house.

        '''
        self.test_attachToSession_new_session()
        self.protocol.terminationDeferred.errback(connectionDone)
        self.assertNotIn(self.sessionID, self.sessions.sessions)
        return self.protocol.terminationDeferred

    def test_attachToSession_existing_session(self):
        '''attachToSession returns the existing session when given a request
        with a duplicate and valid session ID.

        '''
        self.test_attachToSession_new_session()
        self.assertTrue(self.sessions.attachToSession(self, self.request))
        self.assertIs(self.sessions.sessions[self.sessionID], self.protocol)
        self.assertEqual(self.recorder.connectionsMadeFromRequest,
                         [self.request, self.request])

    def test_writeToSession_returns_false(self):
        '''writeToSession with an invalid session ID returns False.'''
        self.assertFalse(self.sessions.writeToSession(DummyRequest([])))

    def test_writeToSession_missing_session(self):
        '''writingToSession with valid but unknown session ID returns False.'''
        unknownSession = self.sessionID * 2

        self.assertFalse(self.sessions.writeToSession(
            DummyRequest([b'server',
                          unknownSession,
                          b'ignored'])))

    def test_writeToSession_existing_session(self):
        '''writeToSession with a valid and known session ID returns True and
        passes the request's content to the session's dataReceived.

        '''
        data = b'some data!'
        self.request.content = io.BytesIO(data)

        self.test_attachToSession_new_session()

        self.assertTrue(self.sessions.writeToSession(self.request))
        self.assertEqual(self.recorder.receivedData, [data])


class XHRSessionTestCase(RequestSessionProtocolWrapperTestCase):

    def makeFactory(self):
        return P.XHRSessionFactory(self.wrappedFactory)

    def test_writeOpen(self):
        '''XHRSession detaches the request immediately after writing an open
        frame.

        '''
        self.protocol.request = self.request
        self.protocol.writeOpen()
        self.assertEqual(self.sessionMachineRecorder.detachCalls, 1)

    def test_writeData(self):
        '''XHRSession detaches the request immediately after writing any
        data frame.

        '''
        self.protocol.request = self.request
        self.protocol.writeData(['ignored'])
        self.assertEqual(self.sessionMachineRecorder.detachCalls, 1)


class XHRStreamingSessionTestCase(RequestSessionProtocolWrapperTestCase):
    maximumBytes = 128

    def makeFactory(self):
        return P.XHRStreamingSessionFactory(maximumBytes=self.maximumBytes,
                                            wrappedFactory=self.wrappedFactory)

    def test_writeOpen(self):
        '''XHRStreamingSession writes a large prelude when establishing a
        connection.

        '''
        self.protocol.request = self.request
        self.protocol.writeOpen()
        self.assertEqual(self.request.written, [b'h' * 2048 + b'\n',
                                                b'o\n'])
        self.assertEqual(self.sessionMachineRecorder.detachCalls, 0)

    def test_completeWrite(self):
        '''XHRStreamingSession detaches the request after writing at least
        maximumBytes.

        '''
        self.protocol.request = self.request

        self.protocol.completeWrite(['ignored'])
        self.assertEqual(self.sessionMachineRecorder.detachCalls, 0)

        self.protocol.completeWrite(['ignored' * self.maximumBytes])
        self.assertEqual(self.sessionMachineRecorder.detachCalls, 1)


class WebSocketProtocolWrapperTestCase(SockJSWireProtocolWrapperTestCase):

    def makeFactory(self):
        return P.WebSocketWrappingFactory(self.wrappedFactory)

    def test_emptyDataReceived(self):
        '''dataReceived silently discards empty strings and does not call the
        wrapped protocol's dataReceived.

        '''
        self.protocol.dataReceived(b'')
        self.assertFalse(self.receivedData)

    def test_badJSONReceived(self):
        '''dataReceived silently closes the connection upon receipt of
        malformed JSON and does not call the wrapped protocol's
        dataReceived.

        '''
        self.protocol.dataReceived(b'!!!')
        self.assertTrue(self.transport.disconnecting)
        self.assertFalse(self.receivedData)


class WebSocketServerProtocolTestCase(unittest.TestCase):

    def setUp(self):
        self.receivedData = []
        self.connectionsLost = []
        self.wrappedFactory = RecordingProtocolFactory(self.receivedData,
                                                       self.connectionsLost)
        buildProtocol = self.wrappedFactory.buildProtocol

        def _buildProtocol(addr):
            self.wrappedProtocol = buildProtocol(addr)
            return self.wrappedProtocol

        self.wrappedFactory.buildProtocol = _buildProtocol

        self.factory = P.WebSocketSessionFactory(self.wrappedFactory)

        self.address = IPv4Address('TCP', '127.0.0.1', 80)
        self.protocol = self.factory.buildProtocol(self.address)

    def makeFakeRequest(self):
        '''This is laborious enough to warrant its own shortcut.'''
        return A.ConnectionRequest(peer='ignored',
                                   headers={},
                                   host='ignored',
                                   path='ignored',
                                   params={},
                                   version=-1,
                                   origin=None,
                                   protocols=[],
                                   extensions=[])

    def test_onConnect_text(self):
        '''onConnect sets _binaryMode to True iff one of the protocols has
        'binary' in it.
        '''
        notBinary = self.makeFakeRequest()
        self.protocol.onConnect(notBinary)
        self.assertFalse(self.protocol._binaryMode)

    def test_onConnect_binary(self):
        '''onConnect sets _binaryMode to True iff one of the protocols has
        'binary' in it.
        '''
        binary = self.makeFakeRequest()
        binary.protocols.append(b'binary')
        self.protocol.onConnect(binary)
        self.assertTrue(self.protocol._binaryMode)

    def test_onOpen(self):
        '''onOpen calls the underlying protocol's makeConnection method with
        _WebSocketServerProtocol instance as the transport.

        '''
        self.protocol.onOpen()
        self.assertEqual(self.wrappedProtocol.connectionMadeCalls, 1)

    def test_write_text(self):
        '''write does base64 encode text data.'''
        # autobahn is very difficult to test -- fake out the
        # sendMessage method

        sentMessages = []

        def recordSendMessage(data, isBinary):
            sentMessages.append((data, isBinary))

        self.test_onConnect_text()
        self.protocol.sendMessage = recordSendMessage

        self.protocol.write(b'some data')
        self.assertEqual(sentMessages, [(b'some data', False)])

    def test_write_binary(self):
        '''write does base64 encode binary data.'''
        sentMessages = []

        def recordSendMessage(data, isBinary):
            sentMessages.append((data, isBinary))

        self.test_onConnect_binary()
        self.protocol.sendMessage = recordSendMessage

        self.protocol.write(b'some data')
        self.assertEqual(sentMessages, [(b'some data', True)])

    def test_onMessage_succeeds(self):
        '''When the received message matches the binary mode of the
        connection, the underlying protocol receives the message as
        deserialized JSON.

        '''
        self.test_onConnect_text()
        self.protocol.onMessage(b'["some data"]', isBinary=False)
        self.assertEqual(self.receivedData, [['some data']])

    def test_onMessage_is_binary_disagreement(self):
        '''When the received message does not match the binary mode of the
        connection, the connection fails and the underlying protocol
        does not receive the message.

        '''
        failedConnectionReasons = []

        def recordFailConnection(reason, message):
            failedConnectionReasons.append(reason)

        self.test_onConnect_binary()
        self.protocol.failConnection = recordFailConnection

        self.protocol.onMessage(b'["some data"]', isBinary=False)
        self.assertEqual(self.receivedData, [])
        self.assertEqual(failedConnectionReasons, [
            self.protocol.CLOSE_STATUS_CODE_UNSUPPORTED_DATA])
