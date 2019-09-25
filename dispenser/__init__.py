"""
The defactor trace set (.trs files) library for reading
Riscure Inspector trace files.
"""

name        = 'dispenser'
__version__ = '0.5.3'
__author__  = 'Kevin Valk'
__contact__ =  'kevin@kevinvalk.nl'
__all__     = [
]

def main():
	import signal
	from dispenser.dispenser import Dispenser

	# Perform all our setup
	dispenser = Dispenser()

	# Add handlers for closing
	signal.signal(signal.SIGINT, dispenser.close)
	signal.signal(signal.SIGTERM, dispenser.close)

	# Start main loop
	try:
		dispenser.loop()
	finally:
		dispenser.close()
