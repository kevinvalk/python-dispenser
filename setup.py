import setuptools
import dispenser

with open('README.md', 'r') as fh:
	long_description = fh.read()

setuptools.setup(
	name                          = dispenser.__name__,
	version                       = dispenser.__version__,
	author                        = dispenser.__author__,
	author_email                  = dispenser.__contact__,
	description                   = (
		'Software used to control the token dispenser'
	),
	long_description              = long_description,
	long_description_content_type = 'text/markdown',
	url                           = 'https://github.com/kevinvalk/python-dispenser',
	packages                      = setuptools.find_packages(),
	install_requires              = [
		'wiringpi',
		'firebase-admin',
		'pi-rc522 @ git+https://github.com/kevinvalk/pi-rc522.git',
	],
	license                       = 'BSD 3-Clause Clear License',
	keywords                      = 'dispenser',
	classifiers                   = [
		'Development Status :: 4 - Beta',
		'Programming Language :: Python :: 3',
		'License :: OSI Approved :: BSD License',
		'Topic :: Utilities',
		'Operating System :: OS Independent',
	],
	project_urls                  = {
		'Bug Reports'  : 'https://github.com/kevinvalk/python-dispenser/issues',
	},
	entry_points                  = {
		'console_scripts': [
			'dispenser = dispenser:main',
		]
	},
)
