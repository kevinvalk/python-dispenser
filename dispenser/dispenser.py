#!/usr/bin/python3
import time
import pirc522
import wiringpi
import logging
import socket
import collections
import dispenser
from wiringpi import HIGH, LOW
from functools import partial
from datetime import timedelta, datetime, timezone
from dispenser.job import Job, JobOnce, JobRunner
from google.cloud import firestore

logFormatter = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(
	format=logFormatter,
	level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load our area
AREA = None
with open('/boot/area', 'r') as f:
	AREA = f.readline().strip(' \r\n')

if AREA is None:
	logger.fatal('No area given')
	exit(-1);
logger.info(f'Dispenser v{dispenser.__version__} for area {AREA}')

# PIN config
LEDS = {
	'holder': 17,
	'reader': 24,
	'ir': 4,
}
PIN_IR_RX = 7
PIN_MOTOR = 18
MOTOR_ON = 100
MOTOR_REVERSE = 200

# Motor on blue uses different algorithm for turning off...
MOTOR_OFF = 0 if AREA == 'blue' else 150

READ_GRACE = timedelta(seconds=3)

T_DETECT_BIG = timedelta(milliseconds=200)
T_DETECT_SMALL = timedelta(milliseconds=100)

T_JAM = timedelta(seconds=2)

def get_ip():
	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		# doesn't even have to be reachable
		s.connect(('10.255.255.255', 1))
		ip = s.getsockname()[0]
	except:
		ip = '127.0.0.1'
	finally:
		s.close()
	return ip

class Dispenser(JobRunner):
	# All variables
	is_calibrating = False
	dispense_no = 0
	current_dispense_no = 0
	leading_edge_at = None
	motor_speed = MOTOR_OFF
	is_recovery = False
	is_closed = False
	previous_ir_state = 0
	previous_edge_time = None
	last_dispense_time = None
	empty_count = 0

	# Different watches
	watch_area = None
	watch_players = None
	is_updating = False

	# Empty list
	is_coin_empty = False
	coin_presences = None

	def __init__(self, **kwargs):

		# Setup all required hardware
		self.reader = pirc522.RFID(pin_irq = None, antenna_gain = 3)
		wiringpi.wiringPiSetupGpio()

		# Setup the motor
		wiringpi.pinMode(PIN_MOTOR, wiringpi.GPIO.PWM_OUTPUT)
		wiringpi.pwmSetMode(wiringpi.GPIO.PWM_MODE_MS)
		wiringpi.pwmSetClock(192)
		wiringpi.pwmSetRange(2000)

		# Setup the LEDs
		for name, pin in LEDS.items():
			wiringpi.pinMode(pin, wiringpi.GPIO.OUTPUT)
			wiringpi.digitalWrite(pin, LOW)

		# Setup IR RX and IR TX
		wiringpi.pinMode(PIN_IR_RX, wiringpi.GPIO.INPUT)
		self.set_led('ir', HIGH)

		self.set_led('reader', HIGH)

		# Setup initial state
		self.coin_presences = collections.deque(maxlen=6)
		self.players = {}
		self.player_details = {}
		self.game = {
			'is_empty': False,
		}

		# Setup Firestore
		self.db = firestore.Client.from_service_account_json('/boot/firebase-credentials.json')
		self.area_ref = self.db.collection('areas').document(AREA)
		self.player_ref = self.db.collection('players')

		# We set our version
		self.area_ref.set({
			'version': dispenser.__version__,
			'is_update': False,
			'ip': get_ip(),
			'is_align': False,
		}, merge = True)

		# Add our watches
		self.job_check_watch()

		# Finally start alignment
		self.align_rotor()

	@Job(minutes = 1)
	def job_check_watch(self):
		# Check if our watch is closed
		if self.watch_area is None or self.watch_area._closed:
			self.watch_area = self.area_ref.on_snapshot(self.on_area_update)
			logger.warning('Restarting area watch')

		# Check if our watch is closed
		if self.watch_players is None or self.watch_players._closed:
			self.watch_players = self.player_ref.on_snapshot(self.on_players_update)
			logger.warning('Restarting players watch')

	# Create a callback on_snapshot function to capture changes
	def on_players_update(self, snapshot, changes, read_time):
		try:
			for change in changes:
				if change.type.name == 'ADDED':
					self.player_details[change.document.id] = change.document.to_dict()
				elif change.type.name == 'MODIFIED':
					self.player_details[change.document.id] = change.document.to_dict()
				elif change.type.name == 'REMOVED':
					del self.player_details[change.document.id]
		except Exception as e:
			logger.exception('Exception in handling players update')

	# Create a callback on_snapshot function to capture changes
	def on_area_update(self, snapshot, changes, read_time):
		try:
			for doc in snapshot:
				data = doc.to_dict()

				# Check for full shutdown
				if 'is_align' in data and data['is_align'] == True:
					self.area_ref.set({
						'is_align': False,
					}, merge = True)

					self.align_rotor()
					return

				# Check for full shutdown
				if 'is_shutdown' in data and data['is_shutdown'] == True:
					self.area_ref.set({
						'is_shutdown': False,
					}, merge = True)

					shutdown()
					return

				# Check if we have to update
				if not self.is_updating and 'is_update' in data and data['is_update'] == True:
					# Remember that we are updating
					self.is_updating = True

					# Trigger self update
					self_update()

					# This should inform systemd to send restart to us
					# we should handle that signal and restart gracefully :)
					return

				# Update our area
				if 'tick_seconds' not in data:
					data['tick_seconds'] = 300

				if 'tick_amount' not in data:
					data['tick_amount'] = 1

				if 'limit' not in data:
					data['limit'] = 25

				self.game['tick_seconds'] = timedelta(seconds=data['tick_seconds'])
				self.game['tick_amount'] = data['tick_amount']
				self.game['limit'] = data['limit']
				# self.job_game_tick.job.update(seconds = data['tick_seconds'] // 4)

				# logger.info(f'Game info, limit: {self.game["limit"]}, tick_seconds: {self.game["tick_seconds"]}, tick_amount: {self.game["tick_amount"]}')

				remote_uids = set()

				# Update our players
				if 'players' in data and isinstance(data['players'], dict):
					for uid, player in data['players'].items():
						remote_uids.add(uid)

						# Make sure dictionary exists
						if uid not in self.players:
							tick = datetime.now(timezone.utc)
							self.players[uid] = {
								'last_read': tick,
								'tick': tick,
								'credit': 0,
								'present': False,
							}

						# Update any changed value
						self.players[uid].update(player)

				# Delete any player that is not on the remote
				for uid in set(self.players.keys()) - remote_uids:
					logger.info(f'Removing local {uid}')
					del self.players[uid]

		except Exception as e:
			logger.exception('Exception in handling area update')

	def close(self, *args):
		if self.is_closed:
			return
		self.is_closed = True
		self.stop()

		logger.info('Closing dispenser')

		# Turnoff LEDs
		for name, pin in LEDS.items():
			wiringpi.digitalWrite(pin, wiringpi.GPIO.LOW)

		# Turnoff motor
		wiringpi.pinMode(PIN_MOTOR, wiringpi.GPIO.OUTPUT)

		self.reader.cleanup()

	def __del__(self):
		self.close()


	def align_rotor(self):
		logger.info('Aligning rotor')
		self.is_calibrating = True
		self.previous_edge_time = None
		self.set_motor(MOTOR_ON)


	def recovery_done(self):
		self.dispense(self.dispense_no - self.current_dispense_no)
		self.is_recovery = False

	@Job(seconds = 1, align = True)
	def job_check_rotor_recovery(self):
		# If we are calibrating or not dispensing, we are not doing anything
		if self.dispense_no <= 0 or self.is_calibrating:
			return

		if (datetime.now(timezone.utc) - last_dispense_time) > T_JAM:
			# Recovery mode
			self.is_recovery = True
			self.set_motor(MOTOR_REVERSE)
			logger.error(f'Jam after {self.current_dispense_no} coins, recovering...')
			JobOnce(self.recovery_done, seconds = 0.4)

	@Job(milliseconds = 4, align = True)
	def job_check_rotor(self):
		# We only check if we are aligning or dispensing
		if self.dispense_no <= 0 and not self.is_calibrating and not self.is_recovery:
			return

		# Initialize
		if self.previous_edge_time is None:
			self.previous_edge_time = datetime.now(timezone.utc)

		ir_state = self.get_ir()

		# Detect raising edge
		if self.previous_ir_state == 0 and ir_state == 1:
			elapsed = (datetime.now(timezone.utc) - self.previous_edge_time)
			# logger.warn(f'Raising edge {elapsed}')

			self.previous_ir_state = 1
			self.previous_edge_time = datetime.now(timezone.utc)

			# Check for our alignment marker
			if elapsed > T_DETECT_BIG:
				# self.coin_presences.append()
				self.on_half_rotation(not self.is_coin_empty)
				self.is_coin_empty = False

		# Detect trailing edge
		elif self.previous_ir_state == 1 and ir_state == 0:
			elapsed = (datetime.now(timezone.utc) - self.previous_edge_time)
			# logger.warn(f'Falling edge {elapsed}')

			self.previous_ir_state = 0
			self.previous_edge_time = datetime.now(timezone.utc)

			# If elapsed is in the slow window, the next coin will be empty
			if elapsed > T_DETECT_SMALL:
				self.is_coin_empty = True



	def on_half_rotation(self, has_coin):
		logger.info(f'Half rotation and coin presence is {has_coin}')

		self.last_dispense_time = datetime.now(timezone.utc)

		if self.is_calibrating:
			self.is_calibrating = False
			self.set_motor(MOTOR_OFF)
			return

		# If we are dispensing
		if self.dispense_no > 0:
			really_empty = False
			if has_coin:
				self.empty_count = 0
				self.current_dispense_no += 1
			else:
				self.empty_count += 1

			logger.info(f'Dispensed {self.current_dispense_no:d}')
			if self.empty_count >= 3 or self.current_dispense_no >= self.dispense_no:
				self.dispense_done(self.current_dispense_no)


	@Job(seconds = 15)
	def job_game_tick(self):
		tick = datetime.now(timezone.utc)

		# logger.info('Main game tick')
		updates = {}
		for uid, player in self.players.items():
			# Skip players that are not present
			if player['present'] != True:
				continue

			# We limit the credits to [0, limit]
			new_credit = max(0, min(player['credit'] + self.game['tick_amount'], self.game['limit']))

			# If there is no change, or if we have already more (and positive tick rate), continue
			if player['credit'] == new_credit or (self.game['tick_amount'] > 0 and player['credit'] > new_credit):
				continue

			# Check for update
			if tick > player['tick'] + self.game['tick_seconds']:
				logger.info(f'Give money to {uid}')

				# Make sure we keep their checkin alignment
				while tick > player['tick'] + self.game['tick_seconds']:
					player['tick'] += self.game['tick_seconds']

				# Update player
				updates[uid] = {
					'credit': firestore.Increment(new_credit - player['credit']),
					'tick': player['tick'],
				}

		# Update everything in one go
		if len(updates) > 0:
			self.area_ref.set({
				'players': updates,
			}, merge = True)


	@Job(milliseconds = 500)
	def job_read_tag(self):
		# Do not read tags if we are going
		if self.motor_speed != MOTOR_OFF:
			return

		# Read the UID
		uid = self.reader.read_id(True)
		if uid is None:
			return

		tick = datetime.now(timezone.utc)

		# We only use string UIDS padded to 14 digits
		uid = f'{uid:014X}'

		# Check if this UID is a valid player
		if uid not in self.player_details:
			logger.error(f'Unknown tag checking in for {uid}')
			self.set_led_flash('reader', 4, 0.1, HIGH)
			return

		# We got a TAG
		if not uid in self.players:
			self.players[uid] = {
				'last_read': tick - READ_GRACE,
				'tick': tick,
				'credit': 0,
				'present': False,
			}

		# Never read to quickly
		if tick >= self.players[uid]['last_read'] + READ_GRACE:
			self.players[uid]['last_read'] = tick
			self.players[uid]['present'] = not self.players[uid]['present']

			if self.players[uid]['present']:
				self.player_checkin(uid)
			else:
				self.player_checkout(uid)
		else:
			logger.info(f'User {uid} still in grace period')


	# Helper functions
	def player_checkin(self, uid):
		logger.info(f'Checkin for {uid}')
		self.set_led_flash('reader', 10, 0.05, HIGH)

		# Checkout this person if in another area
		if (
			'area' in self.player_details[uid] and
			self.player_details[uid]['area'] is not None and
			self.player_details[uid]['area'] != AREA
			):
			logger.info(f'Checking player out at {self.player_details[uid]["area"]}')
			self.db.collection('areas').document(self.player_details[uid]['area']).set({
				'players': {
					uid: {
						'present': False,
					}
				}
			}, merge = True)

		# Update player and area
		self.player_ref.document(uid).set({
			'area': AREA,
		}, merge = True)

		self.area_ref.set({
			'players': {
				uid: {
					'present': True,
					'name': self.player_details[uid]['name'],
					'checkin': firestore.SERVER_TIMESTAMP,
					'tick': firestore.SERVER_TIMESTAMP,
					'credit': firestore.Increment(0),
				}
			}
		}, merge = True)


	def player_checkout(self, uid):
		# Prevent any possible collision here
		if uid not in self.players:
			logger.error('Checking out player that does not exists...')
			return

		logger.info(f'Checkout for {uid} with credit {self.players[uid]["credit"]}')

		if self.players[uid]['credit'] <= 0:
			self.set_led_flash('reader', 10, 0.05, HIGH)
		else:
			self.current_uid = uid
			self.dispense(self.players[uid]['credit'])

		# Flag the player locally to not present to avoid giving more money
		self.players[uid]['present'] = False

	def set_motor(self, speed: int):
		self.motor_speed = speed

		# # We reverse a bit
		# if speed == MOTOR_OFF:
		# 	wiringpi.pwmWrite(PIN_MOTOR, MOTOR_REVERSE)
		# 	time.sleep(0.3)

		wiringpi.pwmWrite(PIN_MOTOR, speed)

	def get_ir(self):
		# We read 10 time with 1 us sleep and get the one that happens the most
		states = []
		for _ in range(0, 10):
			states.append(wiringpi.digitalRead(PIN_IR_RX))
			time.sleep(1 / (1000 * 1000))
		return HIGH if states.count(HIGH) > states.count(LOW) else LOW

	def set_led(self, led : str, value):
		if led not in LEDS:
			raise ValueError(f'LED {led} does not exist')

		logger.debug(f'Setting LED {led} to {value}')
		wiringpi.digitalWrite(LEDS[led], value)

	def dispense(self, amount : int):
		logger.info(f'Dispensing {amount:d}')
		if amount <= 0:
			return

		self.dispense_no = amount
		self.current_dispense_no = 0
		self.previous_edge_time = None
		self.last_dispense_time = datetime.now(timezone.utc)

		# Start the motor
		self.set_motor(MOTOR_ON)
		self.set_led('reader', LOW)
		self.set_led('holder', HIGH)

	def dispense_done(self, amount):
		# Cleanup and turnoff the LED
		# By going a bit longer, we always make a nice half turn
		self.set_motor(MOTOR_OFF)
		JobOnce(lambda: self.set_led('holder', LOW), seconds = 3)
		JobOnce(lambda: self.set_led('reader', HIGH), seconds = 3)

		# Notify server of departure
		# Raise a flag that we are empty
		self.game['is_empty'] = amount != self.dispense_no

		# Check if we are empty, if so, we reduce credit
		if self.game['is_empty']:
			logger.info(f'We are empty, we only dispensed {amount} coins')
			# Only reduce
			self.area_ref.set({
				'paid': firestore.Increment(amount),
				'is_empty': self.game['is_empty'],
				'players': {
					self.current_uid: {
						'present': False,
						'credit': firestore.Increment(-amount),
					}
				}
			}, merge = True)
		else:
			logger.info(f'Dispense done, gave {amount} coins')
			self.area_ref.update({
				'paid': firestore.Increment(amount),
				'is_empty': self.game['is_empty'],
				f'players.{self.current_uid}': firestore.DELETE_FIELD,
			})
			del self.players[self.current_uid]

		# Finally, remove player from area
		self.player_ref.document(self.current_uid).set({
			'area': None,
			AREA: firestore.Increment(amount),
		}, merge = True)

		# Our flag that we are not dispensing
		self.current_uid = None
		self.dispense_no = 0

	def set_led_flash(self, led : str, amount : int, seconds : int, end_value : int, value : int = LOW):
		self.set_led(led, value)
		v = HIGH if value == LOW else LOW

		if amount > 0:
			JobOnce(partial(self.set_led_flash, led, amount - 1, seconds, end_value, v), seconds = seconds)
		else:
			JobOnce(partial(self.set_led, led, end_value), seconds = seconds)

def shutdown():
	import subprocess

	logger.info('Shutting down')
	subprocess.Popen(['shutdown', '-h', '-P', 'now']).wait()

def self_update():
	import subprocess

	logger.info('Updating installed package')
	subprocess.Popen(['pip3', 'install', '-U', 'git+https://github.com/kevinvalk/python-dispenser.git']).wait()

	logger.info('Rebooting the dispenser')
	subprocess.Popen(['systemctl', 'restart', 'dispenser']).wait()

	logger.info('Self update done')
