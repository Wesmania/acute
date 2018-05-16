# © 2013 Mark Harviston <mark.harviston@gmail.com>
# © 2014 Arve Knudsen <arve.knudsen@gmail.com>
# BSD License
import asyncio
import logging
import sys
import os
import ctypes
import multiprocessing
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import socket
import subprocess
import weakref
import gc

import quamash

import pytest


from PyQt5.QtCore import QObject, pyqtSignal


@pytest.fixture
def loop(request, application):
	lp = quamash.QEventLoop(application)
	asyncio.set_event_loop(lp)

	additional_exceptions = []

	def fin():
		sys.excepthook = orig_excepthook

		try:
			lp.close()
		finally:
			asyncio.set_event_loop(None)

		for exc in additional_exceptions:
			if (
				os.name == 'nt' and
				isinstance(exc['exception'], WindowsError) and
				exc['exception'].winerror == 6
			):
				# ignore Invalid Handle Errors
				continue
			raise exc['exception']

	def except_handler(loop, ctx):
		additional_exceptions.append(ctx)

	def excepthook(type, *args):
		lp.stop()
		orig_excepthook(type, *args)

	orig_excepthook = sys.excepthook
	sys.excepthook = excepthook
	lp.set_exception_handler(except_handler)

	request.addfinalizer(fin)
	return lp


@pytest.fixture(
	params=[ThreadPoolExecutor, ProcessPoolExecutor],
)
def executor(request):
	exc_cls = request.param
	if exc_cls is None:
		return None

	exc = exc_cls(1)  # FIXME? fixed number of workers?
	request.addfinalizer(exc.shutdown)
	return exc


ExceptionTester = type('ExceptionTester', (Exception,), {})  # to make flake8 not complain


class TestCanRunTasksInExecutor:

	"""
	Test Cases Concerning running jobs in Executors.

	This needs to be a class because pickle can't serialize closures,
	but can serialize bound methods.
	multiprocessing can only handle pickleable functions.
	"""

	def test_can_run_tasks_in_executor(self, loop, executor):
		"""Verify that tasks can be run in an executor."""
		logging.debug('Loop: {!r}'.format(loop))
		logging.debug('Executor: {!r}'.format(executor))

		manager = multiprocessing.Manager()
		was_invoked = manager.Value(ctypes.c_int, 0)
		logging.debug('running until complete')
		loop.run_until_complete(self.blocking_task(loop, executor, was_invoked))
		logging.debug('ran')

		assert was_invoked.value == 1

	def test_can_handle_exception_in_executor(self, loop, executor):
		with pytest.raises(ExceptionTester) as excinfo:
			loop.run_until_complete(asyncio.wait_for(
				loop.run_in_executor(executor, self.blocking_failure),
				timeout=3.0,
			))

		assert str(excinfo.value) == 'Testing'

	def blocking_failure(self):
		logging.debug('raising')
		try:
			raise ExceptionTester('Testing')
		finally:
			logging.debug('raised!')

	def blocking_func(self, was_invoked):
		logging.debug('start blocking_func()')
		was_invoked.value = 1
		logging.debug('end blocking_func()')

	@asyncio.coroutine
	def blocking_task(self, loop, executor, was_invoked):
		logging.debug('start blocking task()')
		fut = loop.run_in_executor(executor, self.blocking_func, was_invoked)
		yield from asyncio.wait_for(fut, timeout=5.0)
		logging.debug('start blocking task()')


class SignalCaller(QObject):
	first_signal = pyqtSignal(int)
	second_signal = pyqtSignal(int)

	def __init__(self):
		QObject.__init__(self)
		self.timer_count = 0

	def immediate(self):
		self.first_signal.emit(1)
		self.second_signal.emit(2)

	def timed(self):
		self.timer_count = 0
		self._timer = self.startTimer(0)

	def timerEvent(self, event):
		self.timer_count += 1
		if self.timer_count == 1:
			self.first_signal.emit(1)
		if self.timer_count == 2:
			self.second_signal.emit(2)
			self.timer_count = 0
			self.killTimer(self._timer)


@pytest.mark.raises(ExceptionTester)
def test_loop_callback_exceptions_bubble_up(loop):
	"""Verify that test exceptions raised in event loop callbacks bubble up."""
	def raise_test_exception():
		raise ExceptionTester("Test Message")
	loop.call_soon(raise_test_exception)
	loop.run_until_complete(asyncio.sleep(.1))


def test_loop_running(loop):
	"""Verify that loop.is_running returns True when running."""
	@asyncio.coroutine
	def is_running():
		nonlocal loop
		assert loop.is_running()

	loop.run_until_complete(is_running())


def test_loop_not_running(loop):
	"""Verify that loop.is_running returns False when not running."""
	assert not loop.is_running()


def test_can_function_as_context_manager(application):
	"""Verify that a QEventLoop can function as its own context manager."""
	with quamash.QEventLoop(application) as loop:
		assert isinstance(loop, quamash.QEventLoop)
		loop.call_soon(loop.stop)
		loop.run_forever()


def test_future_not_done_on_loop_shutdown(loop):
	"""Verify RuntimError occurs when loop stopped before Future completed with run_until_complete."""
	loop.call_later(.1, loop.stop)
	fut = asyncio.Future()
	with pytest.raises(RuntimeError):
		loop.run_until_complete(fut)


def test_call_later_must_not_coroutine(loop):
	"""Verify TypeError occurs call_later is given a coroutine."""
	mycoro = asyncio.coroutine(lambda: None)

	with pytest.raises(TypeError):
		loop.call_soon(mycoro)


def test_call_later_must_be_callable(loop):
	"""Verify TypeError occurs call_later is not given a callable."""
	not_callable = object()
	with pytest.raises(TypeError):
		loop.call_soon(not_callable)


def test_call_at(loop):
	"""Verify that loop.call_at works as expected."""
	def mycallback():
		nonlocal was_invoked
		was_invoked = True
	was_invoked = False

	loop.call_at(loop.time() + .05, mycallback)
	loop.run_until_complete(asyncio.sleep(.1))

	assert was_invoked


def test_get_set_debug(loop):
	"""Verify get_debug and set_debug work as expected."""
	loop.set_debug(True)
	assert loop.get_debug()
	loop.set_debug(False)
	assert not loop.get_debug()


@pytest.fixture
def sock_pair(request):
	"""Create socket pair.

	If socket.socketpair isn't available, we emulate it.
	"""
	def fin():
		if client_sock is not None:
			client_sock.close()
		if srv_sock is not None:
			srv_sock.close()

	client_sock = srv_sock = None
	request.addfinalizer(fin)

	# See if socketpair() is available.
	have_socketpair = hasattr(socket, 'socketpair')
	if have_socketpair:
		client_sock, srv_sock = socket.socketpair()
		return client_sock, srv_sock

	# Create a non-blocking temporary server socket
	temp_srv_sock = socket.socket()
	temp_srv_sock.setblocking(False)
	temp_srv_sock.bind(('', 0))
	port = temp_srv_sock.getsockname()[1]
	temp_srv_sock.listen(1)

	# Create non-blocking client socket
	client_sock = socket.socket()
	client_sock.setblocking(False)
	try:
		client_sock.connect(('localhost', port))
	except socket.error as err:
		# Error 10035 (operation would block) is not an error, as we're doing this with a
		# non-blocking socket.
		if err.errno != 10035:
			raise

	# Use select to wait for connect() to succeed.
	import select
	timeout = 1
	readable = select.select([temp_srv_sock], [], [], timeout)[0]
	if temp_srv_sock not in readable:
		raise Exception('Client socket not connected in {} second(s)'.format(timeout))
	srv_sock, _ = temp_srv_sock.accept()

	return client_sock, srv_sock


@pytest.mark.xfail(
	'sys.version_info < (3,4)',
	reason="Doesn't work on python older than 3.4",
)
def test_exception_handler(loop):
	handler_called = False
	coro_run = False
	loop.set_debug(True)

	@asyncio.coroutine
	def future_except():
		nonlocal coro_run
		coro_run = True
		loop.stop()
		raise ExceptionTester()

	def exct_handler(loop, data):
		nonlocal handler_called
		handler_called = True

	loop.set_exception_handler(exct_handler)
	asyncio.async(future_except())
	loop.run_forever()

	assert coro_run
	assert handler_called


def test_exception_handler_simple(loop):
	handler_called = False

	def exct_handler(loop, data):
		nonlocal handler_called
		handler_called = True

	loop.set_exception_handler(exct_handler)
	fut1 = asyncio.Future()
	fut1.set_exception(ExceptionTester())
	asyncio.async(fut1)
	del fut1
	loop.call_later(0.1, loop.stop)
	loop.run_forever()
	assert handler_called


def test_not_running_immediately_after_stopped(loop):
	@asyncio.coroutine
	def mycoro():
		assert loop.is_running()
		yield from asyncio.sleep(0)
		loop.stop()
		assert not loop.is_running()
	assert not loop.is_running()
	loop.run_until_complete(mycoro())
	assert not loop.is_running()


def test_can_await_signal(loop):
	async def mycoro():
		caller = SignalCaller()
		sigs = quamash.AsyncSignals([caller.first_signal])
		caller.timed()
		(sender, result) = await sigs
		number, = result
		assert sender == 0
		assert number == 1
	loop.run_until_complete(mycoro())


def test_signals_delivered_before_await_work(loop):
	async def mycoro():
		caller = SignalCaller()
		sigs = quamash.AsyncSignals([caller.first_signal])
		caller.immediate()
		(sender, result) = await sigs
		number, = result
		assert sender == 0
		assert number == 1
	loop.run_until_complete(mycoro())


def test_can_await_signal_multiple_times(loop):
	async def mycoro():
		caller = SignalCaller()
		sigs = quamash.AsyncSignals([caller.first_signal])
		sigs2 = quamash.AsyncSignals([caller.second_signal])
		caller.timed()
		(sender, result) = await sigs
		number, = result
		assert sender == 0
		assert number == 1
		(sender, result) = await sigs2
		number, = result
		assert sender == 0
		assert number == 2
	loop.run_until_complete(mycoro())


def test_can_await_multiple_signals(loop):
	async def mycoro():
		caller = SignalCaller()
		sigs = quamash.AsyncSignals([caller.first_signal, caller.second_signal])
		caller.timed()
		(sender, result) = await sigs
		number, = result
		assert sender == 0
		assert number == 1
	loop.run_until_complete(mycoro())


def test_multiple_signals_get_queued(loop):
	async def mycoro():
		caller = SignalCaller()
		sigs = quamash.AsyncSignals([caller.first_signal, caller.second_signal])
		caller.immediate()
		(sender, result) = await sigs
		number, = result
		assert sender == 0
		assert number == 1
		(sender, result) = await sigs
		number, = result
		assert sender == 1
		assert number == 2
	loop.run_until_complete(mycoro())


def test_async_signals_get_cleaned_up(loop):
	caller = SignalCaller()
	sigs = quamash.AsyncSignals([caller.first_signal, caller.second_signal])

	async def mycoro(caller, sigs):
		caller.immediate()
		(sender, result) = await sigs
		(sender, result) = await sigs

	f = asyncio.ensure_future(mycoro(caller, sigs))
	w_sigs = weakref.ref(sigs)
	w_caller = weakref.ref(caller)
	del sigs
	del caller

	assert w_sigs() is not None
	assert w_caller() is not None
	loop.run_until_complete(f)
	gc.collect()
	assert w_sigs() is None
	assert w_caller() is None
