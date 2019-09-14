#!/usr/bin/python3

import pirc522
import wiringpi
from wiringpi import HIGH, LOW

from google.cloud import firestore

import time
from functools import partial
from datetime import timedelta, datetime, timezone
from dispenser.job import Job, JobOnce, JobRunner

import logging
logFormatter = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(
	format=logFormatter,
	level=logging.INFO
	# handlers=[
	# 	logging.FileHandler("run.log"),
	# 	logging.StreamHandler(),
	# ]
)
logger = logging.getLogger(__name__)

# PIN config
LEDS = {
	'holder': 17,
	'reader': 24,
	'ir': 4,
}
PIN_IR_RX = 7
PIN_MOTOR = 18
MOTOR_ON = 70
MOTOR_OFF = 0

AREA = 'green'


READ_GRACE = timedelta(seconds=3)

PERCENTAGE_EMPTY = 20
T_DETECT_EMPTY = timedelta(milliseconds=310)
T_DETECT_BIG = timedelta(milliseconds=340)

# Calc
T_DETECT_EMPTY_S = T_DETECT_EMPTY * (1 - PERCENTAGE_EMPTY / 100)
T_DETECT_EMPTY_B = T_DETECT_EMPTY * (1 + PERCENTAGE_EMPTY / 100)


logger.debug(f'Range EMPTY {T_DETECT_EMPTY_S} - {T_DETECT_EMPTY_B}')

db = firestore.Client.from_service_account_json('/boot/firebase-credentials.json')



# TODO: Synchronize local players with remote tracked players on boot!

class Dispenser(JobRunner):
	# All variables
	is_calibrating = False
	dispense_no = 0
	current_dispense_no = 0
	leading_edge_at = None
	motor_speed = MOTOR_OFF
	is_recovery = False
	is_closed = False

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
		self.players = {}
		self.game = {
			'is_empty': False,
		}

		self.align_rotor()

		# Now subscribe to updates on the DB
		self.area_ref = db.collection('areas').document(AREA)
		self.doc_watch = self.area_ref.on_snapshot(self.on_area_update)


	# Create a callback on_snapshot function to capture changes
	def on_area_update(self, snapshot, changes, read_time):
		for doc in snapshot:
			# print('Received document snapshot: {}'.format(doc.id))
			logger.debug('Updating from firestore')
			data = doc.to_dict()

			# Update our area
			self.game['tick_seconds'] = timedelta(seconds=data['tick_seconds'])
			self.job_game_tick.job.update(seconds = data['tick_seconds'] // 4)

			remote_uids = set()

			# Update our players
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

	def close(self):
		if self.is_closed:
			return
		self.is_closed = True

		# Turnoff LEDs
		for name, pin in LEDS.items():
			wiringpi.digitalWrite(pin, wiringpi.GPIO.LOW)

		# Turnoff motor
		wiringpi.pinMode(PIN_MOTOR, wiringpi.GPIO.OUTPUT)

		self.reader.cleanup()

	def __del__(self):
		self.close()


	def align_rotor(self):
		self.is_calibrating = True
		self.previous_edge_time = datetime.now(timezone.utc)
		self.set_motor(MOTOR_ON)


	def recovery_done(self):
		self.dispense(self.dispense_no - self.current_dispense_no)
		self.is_recovery = False

	previous_dispense_no = None
	@Job(seconds = 2)
	def job_check_rotor_recovery(self):
		# Check if we are dispensing
		if self.dispense_no <= 0 and not self.is_calibrating:
			return

		if self.previous_dispense_no == self.current_dispense_no:
			# Recovery mode
			self.is_recovery = True
			self.set_motor(200)
			logger.error(f'Jam after {self.current_dispense_no} coins, recovering...')
			JobOnce(self.recovery_done, seconds = 0.5)

		self.previous_dispense_no = self.current_dispense_no


	pattern_step = -1
	previous_ir_state = None
	previous_edge_time = None
	@Job(milliseconds = 5, align = False)
	def job_check_rotor(self):
		# We only check if we are aligning or dispensing
		if self.dispense_no <= 0 and not self.is_calibrating and not self.is_recovery:
			return

		# Initialize
		if self.previous_ir_state is None:
			self.previous_ir_state = self.get_ir()
			self.previous_edge_time = datetime.now(timezone.utc)

		# We read 10 time with 1 us sleep and get the one that happens the most
		states = []
		for _ in range(0, 10):
			states.append(self.get_ir())
			time.sleep(1 / (1000 * 1000))
		ir_state = HIGH if states.count(HIGH) > states.count(LOW) else LOW


		# Detect raising edge
		if self.previous_ir_state == 0 and ir_state == 1:
			elapsed = (datetime.now(timezone.utc) - self.previous_edge_time)
			# logger.debug(f'Raising edge {elapsed}')

			self.previous_ir_state = 1
			self.previous_edge_time = datetime.now(timezone.utc)

			# Check for our alignment marker
			if elapsed > T_DETECT_BIG:
				self.on_half_rotation()

		# Detect trailing edge
		elif self.previous_ir_state == 1 and ir_state == 0:
			elapsed = (datetime.now(timezone.utc) - self.previous_edge_time)
			# logger.debug(f'Falling edge {elapsed}')

			self.previous_ir_state = 0
			self.previous_edge_time = datetime.now(timezone.utc)

			# Check if in small window
			if self.dispense_no > 0 and elapsed > T_DETECT_EMPTY_S and elapsed < T_DETECT_EMPTY_B:
				# Raise a flag that we are empty
				self.game['is_empty'] = True

				# We restore the player, but with less money
				player_info = next(iter(self.player_snapshot.values()))
				player_info['credit'] -= self.current_dispense_no
				player_info['present'] = True
				self.area_ref.set({
					'is_empty': self.game['is_empty'],
					'players': self.player_snapshot
				}, merge = True)

				# we are stealing credit from player
				self.dispense_done()


	def on_half_rotation(self):
		logger.debug(f'Half rotation')

		if self.is_calibrating:
			self.is_calibrating = False
			self.set_motor(MOTOR_OFF)


		if self.dispense_no > 0:
			self.current_dispense_no += 1
			logger.debug(f'Dispensed {self.current_dispense_no:d}')
			if self.current_dispense_no >= self.dispense_no:
				self.dispense_done()


	@Job(seconds = 30, align = False)
	def job_game_tick(self):
		tick = datetime.now(timezone.utc)

		# logger.info('Main game tick')
		updates = {}
		for uid, player in self.players.items():
			# Skip players that are not present
			if not player['present']:
				continue

			# Check for update
			if tick > player['tick'] + self.game['tick_seconds']:
				logger.debug(f'Give money to {uid}')

				# Make sure we keep their checkin alignment
				while tick > player['tick'] + self.game['tick_seconds']:
					player['tick'] += self.game['tick_seconds']

				# Update player
				updates[uid] = {
					'credit': firestore.Increment(1),
					'tick': player['tick'],
				}

		# Update everything in one go
		if len(updates) > 0:
			self.area_ref.set({
				'players': updates,
			}, merge = True)


	@Job(milliseconds = 350, align = True)
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


	# Helper functions
	def player_checkin(self, uid):
		logger.debug(f'Checkin for {uid}')
		self.set_led_flash('reader', 10, 0.05, HIGH)

		# Update player
		self.area_ref.set({
			'players': {
				uid: {
					'present': True,
					'checkin': firestore.SERVER_TIMESTAMP,
					'tick': firestore.SERVER_TIMESTAMP,
				}
			}
		}, merge = True)


	def player_checkout(self, uid):
		logger.debug(f'Checkout for {uid}')
		self.set_led_flash('reader', 0, 4, HIGH)

		self.dispense(self.players[uid]['credit'])

		# Remove player
		self.player_snapshot = {uid: self.players[uid]}
		self.game['is_empty'] = False
		self.area_ref.update({
			'is_empty': self.game['is_empty'],
			f'players.{uid}': firestore.DELETE_FIELD,
		})
		del self.players[uid]

	def set_motor(self, speed: int):
		# logger.debug(f'Setting motor {speed:d}')
		self.motor_speed = speed
		wiringpi.pwmWrite(PIN_MOTOR, speed)

	def get_ir(self):
		return wiringpi.digitalRead(PIN_IR_RX)

	def set_led(self, led : str, value):
		if led not in LEDS:
			raise ValueError(f'LED {led} does not exist')

		# logger.debug(f'Setting LED {led} to {value}')
		wiringpi.digitalWrite(LEDS[led], value)

	def dispense(self, amount : int):
		logger.info(f'Dispensing {amount:d}')
		if amount <= 0:
			return

		self.dispense_no = amount
		self.current_dispense_no = 0
		self.previous_dispense_no = None
		self.previous_edge_time = datetime.now(timezone.utc)

		# Start the motor
		self.set_motor(MOTOR_ON)
		self.set_led('holder', HIGH)

	def dispense_done(self):
		# Cleanup and turnoff the LED
		self.set_motor(MOTOR_OFF)
		JobOnce(lambda: self.set_led('holder', LOW), seconds = 3)
		self.dispense_no = 0

	def set_led_flash(self, led : str, amount : int, seconds : int, end_value : int, value : int = LOW):
		self.set_led(led, value)
		v = HIGH if value == LOW else LOW

		if amount > 0:
			JobOnce(partial(self.set_led_flash, led, amount - 1, seconds, end_value, v), seconds = seconds)
		else:
			JobOnce(partial(self.set_led, led, end_value), seconds = seconds)


def main():
	# Perform all our setup
	dispenser = Dispenser()

	# Start main loop
	dispenser.loop()

	dispenser.close()

if __name__ == '__main__':
	main()
