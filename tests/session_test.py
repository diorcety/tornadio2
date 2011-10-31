from collections import deque

from nose.tools import ok_, eq_, raises

from tornadio2 import session, proto, conn

from simplejson import JSONDecodeError


class DummyServer(object):
	def __init__(self, conn):
		self._connection = conn
		self.settings = dict(
				session_check_interval=15,
				session_expiry=30,
				heartbeat_interval=12,
				enabled_protocols=['websocket', 'flashsocket','xhr-polling',
								   'xhr-multipart', 'jsonp-polling', 'htmlfile'],
				xhr_polling_timeout=20								  
		)

	def create_session(self):
		return session.Session(self._connection,
							   self,
							   None,
							   self.settings.get('session_expiry'))


class DummyTransport(object):
	def __init__(self, session):
		self.session = session
		self.outgoing = deque()
		self.is_open = True

	def send_messages(self, messages):
		self.outgoing.extend(messages)

	def session_closed(self):
		self.is_open = False

	# Manipulation
	def recv(self, message):
		self.session.raw_message(message)

	def pop_outgoing(self):
		return self.outgoing.popleft()


class DummyConnection(conn.SocketConnection):
	def __init__(self, session, endpoint=None):
		super(DummyConnection, self).__init__(session, endpoint)

		self.is_open = False

		self.incoming = deque()
		self.events = deque()
		
		self.open_args = []
		self.open_kwargs = {}		

	def on_open(self, *args, **kwargs):
		self.is_open = True

		self.open_args = list(args)
		self.open_kwargs = kwargs

	def on_message(self, message):
		self.incoming.append(message)
		self.send(message)

	def on_event(self, name, **kwargs):
		self.events.append((name, kwargs))
		self.emit(name, **kwargs)

	def on_close(self):
		self.is_open = False

	def get_endpoint(self, name):
		return DummyConnection

	# Helpers
	def pop_incoming(self):
		return self.incoming.popleft()

	def pop_event(self):
		return self.events.popleft()


def _get_test_environment(*args, **kwargs):
	# Create test environment
	server = DummyServer(DummyConnection)
	session = server.create_session()
	transport = DummyTransport(session)

	conn = session.conn

	session.open(10,a=10)

	# Attach handler and check if it was attached
	session.set_handler(transport)
	eq_(session.handler, transport)

	# Check if connection event was submitted
	session.flush()
	eq_(transport.pop_outgoing(), '1::')

	return server, session, transport, conn	


def test_session_attach():	
	# Create environment
	server, session, transport, conn = _get_test_environment(10, a=10)

	# Check if connection opened
	eq_(conn.is_open, True)
	eq_(conn.open_args, [10])
	eq_(conn.open_kwargs, {'a':10})

	# Send message and check if it was handled by connection
	transport.recv(proto.message(None, 'abc'))

	# Check if incoming queue has abc
	eq_(conn.pop_incoming(), 'abc')

	# Check if outgoing transport has abc
	eq_(transport.pop_outgoing(), '3:::abc')

	# Close session
	conn.close()

	# Check if it sent disconnect packet to the client
	eq_(transport.pop_outgoing(), '0::')

	# Detach
	session.remove_handler(transport)
	eq_(session.handler, None)

	# Check if session is still open
	eq_(transport.is_open, False)
	eq_(conn.is_open, False)
	eq_(session.is_closed, True)


def test_client_disconnect():
	# Create environment
	server, session, transport, conn = _get_test_environment()

	# Send disconnect message
	transport.recv(proto.disconnect())

	# Check if connection was closed
	eq_(transport.pop_outgoing(), '0::')

	eq_(conn.is_open, False)
	eq_(session.is_closed, True)


def test_json():
	# Create environment
	server, session, transport, conn = _get_test_environment()

	# Send json message
	transport.recv(proto.message(None, dict(a=10, b=20)))

	# Check incoming message
	eq_(conn.pop_incoming(), dict(a=10,b=20))

	# Check outgoing message
	eq_(transport.pop_outgoing(), proto.message(None, dict(a=10, b=20)))


def test_event():
	# Create environment
	server, session, transport, conn = _get_test_environment()

	# Send event
	transport.recv(proto.event(None, 'test', a=10, b=20))

	# Check incoming
	eq_(conn.pop_event(), ('test', dict(a=10, b=20)))

	# Check outgoing
	eq_(transport.pop_outgoing(), proto.event(None, 'test', a=10, b=20))


@raises(JSONDecodeError)
def test_json_error():
	# Create environment
	server, session, transport, conn = _get_test_environment()

	# Send malformed JSON message
	transport.recv('4:::{asd')


def test_endpoint():
	# Create environment
	server, session, transport, conn = _get_test_environment()

	# Connect endpoint
	transport.recv(proto.connect('/test?a=123&b=456'))

	# Verify that client received connect message
	eq_(transport.pop_outgoing(), '1::/test')

	# Verify that connection object was created
	conn_test = session.endpoints['/test']	
	eq_(conn_test.endpoint, '/test')
	eq_(conn_test.is_open, True)
	eq_(conn_test.open_kwargs, dict(a='123',b='456'))

	# Send message to endpoint and verify that it was received
	transport.recv(proto.message('/test', 'abc'))
	eq_(conn_test.pop_incoming(), 'abc')
	eq_(transport.pop_outgoing(), '3::/test:abc')

	# Close endpoint connection from client
	transport.recv(proto.disconnect('/test'))

	# Verify that everything was cleaned up
	eq_(transport.pop_outgoing(), '0::/test')
	eq_(conn_test.is_open, False)
	eq_(conn.is_open, True)
	eq_(session.is_closed, False)

	eq_(session.endpoints, dict())

	# Open another endpoint connection
	transport.recv(proto.connect('/test2'))

	# Verify that client received connect message
	eq_(transport.pop_outgoing(), '1::/test2')

	# Get connection
	conn_test = session.endpoints['/test2']
	eq_(conn_test.open_kwargs, dict())

	# Close main connection
	transport.recv(proto.disconnect())

	# Check if connections were closed and sent out
	eq_(transport.pop_outgoing(), '0::/test2')
	eq_(transport.pop_outgoing(), '0::')

	eq_(conn_test.is_open, False)
	eq_(conn.is_open, False)
	eq_(session.is_closed, True)

def test_invalid_endpoint():
	# Create environment
	server, session, transport, conn = _get_test_environment()

	# Send message to unconnected endpoint
	transport.recv(proto.message('test', 'abc'))

	# Check if message was received by default endpoint
	eq_(len(conn.incoming), 0)

def test_ack():
	# Create environment
	server, session, transport, conn = _get_test_environment()

	# Send message with ACK
	transport.recv(proto.message(None, 'abc', 1))

	# Check that message was received by the connection
	eq_(conn.pop_incoming(), 'abc')

	# Check for ACK
	eq_(transport.pop_outgoing(), '3:::abc')
	eq_(transport.pop_outgoing(), '6:::1')

	# Send with ACK
	executed = False
	def handler(message):
		eq_(message, 'abc')

		conn.send('yes')

	conn.send('abc', handler)

	eq_(transport.pop_outgoing(), '3:1::abc')

	# Send ACK from client
	transport.recv('6:::1')

	# Check if handler was called
	eq_(transport.pop_outgoing(), '3:::yes')
