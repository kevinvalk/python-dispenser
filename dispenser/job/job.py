import time
import random
import types
import functools
from datetime import timedelta, datetime

jobs = []

def is_lambda_function(obj):
	return isinstance(obj, types.LambdaType) and obj.__name__ == "<lambda>"

class Job():
	def __init__(self, **kwargs):
		self.job = {}
		self.kwargs = kwargs

	def __align(self):
		t = datetime.now().timestamp()
		self.job['tock'] = datetime.fromtimestamp(
			t - (t % self.job['interval'].total_seconds()) + self.job['interval'].total_seconds()
		)

	def __update(self, f, **kwargs):
		# Build the timedelta
		timedelta_args = {}
		for kwkey in ['days', 'seconds', 'microseconds', 'milliseconds', 'minutes', 'hours', 'weeks']:
			timedelta_args[kwkey] = kwargs.pop(kwkey, 0)
		interval = timedelta(**timedelta_args)

		# Prepare our information
		self.job['function'] = f
		self.job['is_standalone'] = is_lambda_function(f) or isinstance(f, functools.partial)
		self.job['tock']     = None
		self.job['disabled'] = False
		self.job['class']    = '' if self.job['is_standalone'] else '.'.join(f.__qualname__.split('.')[:-1])
		self.job['interval'] = interval

		# Perform any optional behavior
		if kwargs.get('align', False):
			self.__align()

		# Finally store the kwargs
		self.kwargs = kwargs

	def __call__(self, f):
		# Update the job information
		self.__update(f, **self.kwargs)

		# Register the job
		jobs.append(self.job)
		f.job = self

		# Just return the function without any markup
		return f

	# Used to change a running job
	def update(self, **kwargs):
		self.kwargs.update(kwargs)
		self.__update(self.job['function'], **self.kwargs)

class JobOnce(Job):
	def __init__(self, f, **kwargs):
		super().__init__(**kwargs)
		self._Job__update(f, **kwargs)

		# Make it into a single shot
		if self.kwargs.get('align', False):
			# Tock is already set to the next aligned hit
			self.job['interval'] = None
		else:
			self.job['tock'] = datetime.now() + self.job['interval']
			self.job['interval'] = None

		# Finally we have to register it ourselves
		jobs.append(self.job)

class JobRunner():
	def loop(self):
		global jobs

		try:
			while True:
				has_disabled = False
				tick = datetime.now()

				for job in jobs:
					# Skip disabled
					if job['disabled']:
						has_disabled = True
						continue

					# Check if this function is part of this instance
					if not job['is_standalone'] and self.__class__.__qualname__ != job['class']:
						continue

					# Initialize begin time for a new job
					if job['tock'] is None:
						job['tock'] = tick

						# Add between zero to one interval to the time
						# this way we hopefully space out jobs a bit
						# more
						job['tock'] += job['interval'] * random.random()

					# Initial run
					if tick >= job['tock']:
						# Perform all tocks even when we miss a few
						while job['interval'] and tick >= job['tock']:
							job['tock'] = job['tock'] + job['interval']

						# Now we call our job
						if job['is_standalone'] or \
							 (hasattr(job['function'], '__self__') and job['function'].__self__ is not None):
							job['function']()
						else:
							job['function'](self)

						# Check if this was a single shot
						if job['interval'] is None:
							job['disabled'] = has_disabled = True

				# We run our loop every ms.
				# This enables us to not to exhaust the CPU!
				time.sleep(1 / 1000)

				# Remove any disabled jobs
				if has_disabled:
					jobs = [job for job in jobs if not job['disabled']]

		except KeyboardInterrupt:
			pass
