# © 2014 Mark Harviston <mark.harviston@gmail.com>
# © 2014 Arve Knudsen <arve.knudsen@gmail.com>
# BSD License
"""Implementation of the PEP 3156 Event-Loop with Qt."""

__author__ = 'Igor Kotrasinski <i.kotrasinsk@gmail.com>, Mark Harviston <mark.harviston@gmail.com>, Arve Knudsen <arve.knudsen@gmail.com>'
__version__ = '0.1.0'
__url__ = 'https://github.com/Wesmania/acute'
__license__ = 'BSD'
__all__ = ['QEventLoop', 'AsyncSignals']

import sys
import asyncio
import time
import logging
logger = logging.getLogger('acute')

from PyQt5 import QtCore, QtWidgets

QApplication = QtWidgets.QApplication

from ._common import with_logger


class Signaller(QtCore.QObject):
	signal = QtCore.pyqtSignal(object, tuple)


@with_logger
class _SimpleTimer(QtCore.QObject):
	def __init__(self):
		super().__init__()
		self.__callbacks = {}
		self._stopped = False

	def add_callback(self, handle, delay=0):
		timerid = self.startTimer(delay * 1000)
		self._logger.debug("Registering timer id {0}".format(timerid))
		assert timerid not in self.__callbacks
		self.__callbacks[timerid] = handle
		return handle

	def timerEvent(self, event):  # noqa: N802
		timerid = event.timerId()
		self._logger.debug("Timer event on id {0}".format(timerid))
		if self._stopped:
			self._logger.debug("Timer stopped, killing {}".format(timerid))
			self.killTimer(timerid)
			del self.__callbacks[timerid]
		else:
			try:
				handle = self.__callbacks[timerid]
			except KeyError as e:
				self._logger.debug(str(e))
				pass
			else:
				if handle._cancelled:
					self._logger.debug("Handle {} cancelled".format(handle))
				else:
					self._logger.debug("Calling handle {}".format(handle))
					handle._run()
			finally:
				del self.__callbacks[timerid]
				handle = None
			self.killTimer(timerid)

	def stop(self):
		self._logger.debug("Stopping timers")
		self._stopped = True


class _DoneSignal(QtCore.QObject):
	done = QtCore.pyqtSignal(object)

	def __init__(self):
		super().__init__()


class SignalMixin:
	def __init__(self):
		self._done_signal = None

	@property
	def done_signal(self):
		if self._done_signal is None:
			self._done_signal = _DoneSignal()
		return self._done_signal.done

	def emit_done_signal(self, result):
		if self._done_signal is not None:
			self.done_signal.emit(result)


class SignalTask(asyncio.Task, SignalMixin):
	def __init__(self, *args, **kwargs):
		asyncio.Task.__init__(self, *args, **kwargs)
		SignalMixin.__init__(self)
		self.add_done_callback(self.emit_done_signal)


class SignalFuture(asyncio.Future, SignalMixin):
	def __init__(self, *args, **kwargs):
		asyncio.Future.__init__(self, *args, **kwargs)
		SignalMixin.__init__(self)
		self.add_done_callback(self.emit_done_signal)


@with_logger
class AsyncSignals(QtCore.QObject):
	done = QtCore.pyqtSignal(object)

	def __init__(self, signals, loop=None):
		QtCore.QObject.__init__(self)
		if loop is not None:
			self._loop = loop
		else:
			self._loop = asyncio.get_event_loop()
		self._callbacks = set()
		self.results = []
		self._logger.debug("Wrapping signals: {}".format(signals))
		for i, s in enumerate(signals):
			def cb(*args, i=i):
				self._called(i, *args)
			s.connect(cb)
			self._callbacks.add((s, cb))

	def _called(self, i, *args):
		self._logger.debug("Signal {} called, emitting done".format(i))
		self.results.append((i, args))
		self.done.emit(self)

	def __await__(self):
		future = self._loop.create_future()
		h = future._loop.call_when_done(self, self._set_future_result, future)
		try:
			self._logger.debug("Awaiting signals")
			yield from future
			items = future.result()
			self._logger.debug("Signal returned: {}".format(items))
			return items
		finally:
			h.cancel()

	def _set_future_result(self, future):
		if future.cancelled():
			self._logger.debug("Future cancelled, not setting result")
			return
		item = self.results.pop(0)
		future.set_result(item)


@with_logger
class AsyncSignalKeeper:
	def __init__(self):
		self._async_signals = {}

	def track(self, handle, asignal):
		self._async_signals[asignal] = handle
		asignal.done.connect(self._signal_done)
		return handle

	def _signal_done(self, asignal):
		self._logger.debug("Received signal done from {}".format(asignal))
		asignal.done.disconnect(self._signal_done)
		handle = self._async_signals.pop(asignal, None)
		if handle is None or handle._cancelled:
			return
		self._logger.debug("Running the handle {}".format(handle))
		handle._run()


@with_logger
class _QEventLoop(asyncio.AbstractEventLoop):

	"""
	Implementation of asyncio event loop that uses the Qt Event loop.

	>>> import asyncio
	>>>
	>>> app = getfixture('application')
	>>>
	>>> @asyncio.coroutine
	... def xplusy(x, y):
	...     yield from asyncio.sleep(.1)
	...     assert x + y == 4
	...     yield from asyncio.sleep(.1)
	>>>
	>>> loop = QEventLoop(app)
	>>> asyncio.set_event_loop(loop)
	>>> with loop:
	...     loop.run_until_complete(xplusy(2, 2))
	"""

	def __init__(self, app=None):
		self.__app = app or QApplication.instance()
		assert self.__app is not None, 'No QApplication has been instantiated'
		self.__is_running = False
		self.__is_closed = False
		self.__debug_enabled = False
		self.__default_executor = None
		self.__exception_handler = None
		self._timer = _SimpleTimer()
		self._async_keeper = AsyncSignalKeeper()
		self.__call_soon_signaller = signaller = Signaller()
		self.__call_soon_signal = signaller.signal
		signaller.signal.connect(lambda callback, args: self.call_soon(callback, *args))

		assert self.__app is not None

		super().__init__()

	def run_forever(self):
		"""Run eventloop forever."""
		self.__is_running = True
		try:
			self._logger.debug('Starting Qt event loop')
			rslt = self.__app.exec_()
			self._logger.debug('Qt event loop ended with result {}'.format(rslt))
			self.__app.processEvents()  # run loop one last time to process all the events
			return rslt
		finally:
			self.__is_running = False

	def run_until_complete(self, future):
		"""Run until Future is complete."""
		self._logger.debug('Running {} until complete'.format(future))
		future = asyncio.async(future, loop=self)

		def stop(*args): self.stop()  # noqa
		future.add_done_callback(stop)
		try:
			self.run_forever()
		finally:
			future.remove_done_callback(stop)
		if not future.done():
			raise RuntimeError('Event loop stopped before Future completed.')

		self._logger.debug('Future {} finished running'.format(future))
		return future.result()

	def stop(self):
		"""Stop event loop."""
		if not self.__is_running:
			self._logger.debug('Already stopped')
			return

		self._logger.debug('Stopping event loop...')
		self.__is_running = False
		self.__app.exit()
		self._logger.debug('Stopped event loop')

	def is_running(self):
		"""Return True if the event loop is running, False otherwise."""
		return self.__is_running

	def is_closed(self):
		return self.__is_closed

	def close(self):
		"""
		Release all resources used by the event loop.

		The loop cannot be restarted after it has been closed.
		"""
		if self.is_running():
			raise RuntimeError("Cannot close a running event loop")
		if self.is_closed():
			return

		self._logger.debug('Closing event loop...')
		if self.__default_executor is not None:
			self.__default_executor.shutdown()

		self._timer.stop()
		self.__app = None
		self.__is_closed = True

	def call_later(self, delay, callback, *args):
		"""Register callback to be invoked after a certain delay."""
		if asyncio.iscoroutinefunction(callback):
			raise TypeError("coroutines cannot be used with call_later")
		if not callable(callback):
			raise TypeError('callback must be callable: {}'.format(type(callback).__name__))

		self._logger.debug(
			'Registering callback {} to be invoked with arguments {} after {} second(s)'
			.format(callback, args, delay))
		return self._add_callback(asyncio.Handle(callback, args, self), delay)

	def call_when_done(self, asignal, callback, *args):
		if asignal.results:
			return self.call_soon(callback, *args)

		"""Register callback to be invoked after a certain delay."""
		if asyncio.iscoroutinefunction(callback):
			raise TypeError("coroutines cannot be used with call_when_done")
		if not callable(callback):
			raise TypeError('callback must be callable: {}'.format(type(callback).__name__))

		self._logger.debug(
			'Registering callback {} to be invoked after a signal'
			.format(callback))
		handle = asyncio.Handle(callback, args, self)
		return self._async_keeper.track(handle, asignal)

	def _add_callback(self, handle, delay=0):
		return self._timer.add_callback(handle, delay)

	def call_soon(self, callback, *args):
		"""Register a callback to be run on the next iteration of the event loop."""
		return self.call_later(0, callback, *args)

	def call_at(self, when, callback, *args):
		"""Register callback to be invoked at a certain time."""
		return self.call_later(when - self.time(), callback, *args)

	def time(self):
		"""Get time according to event loop's clock."""
		return time.monotonic()

	# Methods for interacting with threads.

	def call_soon_threadsafe(self, callback, *args):
		"""Thread-safe version of call_soon."""
		self.__call_soon_signal.emit(callback, args)

	def run_in_executor(self, executor, callback, *args):
		"""Run callback in executor.

		If no executor is provided, the default executor will be used, which defers execution to
		a background thread.
		"""
		self._logger.debug('Running callback {} with args {} in executor'.format(callback, args))
		if isinstance(callback, asyncio.Handle):
			assert not args
			assert not isinstance(callback, asyncio.TimerHandle)
			if callback._cancelled:
				f = asyncio.Future()
				f.set_result(None)
				return f
			callback, args = callback.callback, callback.args

		if executor is None:
			self._logger.debug('Using default executor')
			executor = self.__default_executor

		if executor is None:
			raise ValueError

		return asyncio.wrap_future(executor.submit(callback, *args))

	def set_default_executor(self, executor):
		self.__default_executor = executor

	# Error handlers.

	def set_exception_handler(self, handler):
		self.__exception_handler = handler

	def default_exception_handler(self, context):
		"""Handle exceptions.

		This is the default exception handler.

		This is called when an exception occurs and no exception
		handler is set, and can be called by a custom exception
		handler that wants to defer to the default behavior.

		context parameter has the same meaning as in
		`call_exception_handler()`.
		"""
		self._logger.debug('Default exception handler executing')
		message = context.get('message')
		if not message:
			message = 'Unhandled exception in event loop'

		try:
			exception = context['exception']
		except KeyError:
			exc_info = False
		else:
			exc_info = (type(exception), exception, exception.__traceback__)

		log_lines = [message]
		for key in [k for k in sorted(context) if k not in {'message', 'exception'}]:
			log_lines.append('{}: {!r}'.format(key, context[key]))

		self.__log_error('\n'.join(log_lines), exc_info=exc_info)

	def call_exception_handler(self, context):
		if self.__exception_handler is None:
			try:
				self.default_exception_handler(context)
			except Exception:
				# Second protection layer for unexpected errors
				# in the default implementation, as well as for subclassed
				# event loops with overloaded "default_exception_handler".
				self.__log_error('Exception in default exception handler', exc_info=True)

			return

		try:
			self.__exception_handler(self, context)
		except Exception as exc:
			# Exception in the user set custom exception handler.
			try:
				# Let's try the default handler.
				self.default_exception_handler({
					'message': 'Unhandled error in custom exception handler',
					'exception': exc,
					'context': context,
				})
			except Exception:
				# Guard 'default_exception_handler' in case it's
				# overloaded.
				self.__log_error(
					'Exception in default exception handler while handling an unexpected error '
					'in custom exception handler', exc_info=True)

	def create_future(self):
		return SignalFuture(loop=self)

	def create_task(self, coro):
		if self.is_closed():
			raise RuntimeError("Event loop is closed")
		task = SignalTask(coro, loop=self)
		if task._source_traceback:
			del task._source_traceback[-1]
		return task

	# Debug flag management.
	def get_debug(self):
		return self.__debug_enabled

	def set_debug(self, enabled):
		self.__debug_enabled = enabled

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.stop()
		self.close()

	@classmethod
	def __log_error(cls, *args, **kwds):
		# In some cases, the error method itself fails, don't have a lot of options in that case
		try:
			cls._logger.error(*args, **kwds)
		except: # noqa E722
			sys.stderr.write('{!r}, {!r}\n'.format(args, kwds))


QEventLoop = _QEventLoop


class _Cancellable:
	def __init__(self, timer, loop):
		self.__timer = timer
		self.__loop = loop

	def cancel(self):
		self.__timer.stop()
