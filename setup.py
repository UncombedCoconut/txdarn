"""
Setup file for txdarn -- SockJS for Twisted.
"""

from setuptools import setup, find_packages

setup(name='txdarn',
      version='17.0.1',
      description="""
      SockJS for modern Twisted.
      """,
      packages=find_packages(),
      zip_safe=False,
      include_package_data=True,
      install_requires=['Twisted>=16.6.0',
                        'constantly',
                        'eliot',
                        'six',
                        'autobahn>=0.17.1',
                        'automat>=0.3.0'])
