#!/usr/bin/python
# -*- coding: utf-8 -*-
from flask import Flask, request, session, abort, g, render_template, url_for
import jinja2 
import os.path
from os import walk
import imp
import random
import string
import logging
import os.path
from logging.handlers import SMTPHandler
from logging.handlers import RotatingFileHandler
from logging import Formatter
import redis
import MySQLdb as mysql

class CortexFlask(Flask):

	# type of workflows
	WF_CREATE        = 1 # create new things
	WF_SYSTEM_ACTION = 2 # perform actions on systems

	# store the list of 'CREATE' workflows
	workflows = []

	# store the list of 'SYSTEM ACTION' workflows
	system_actions = []

	################################################################################

	def __init__(self, init_object_name):
		"""Constructor for the CortexFlask application. Reads the config, sets
		up logging, configures Jinja and Flask."""

		# Call the superclass (Flask) constructor
		super(CortexFlask, self).__init__(init_object_name)

		# CSRF exemption support
		self._exempt_views = set()
		self.before_request(self._csrf_protect)

		# CSRF token function in templates
		self.jinja_env.globals['csrf_token'] = self._generate_csrf_token

		# Load the __init__.py config defaults
		self.config.from_object("cortex.defaultcfg")

		# Load system config file
		self.config.from_pyfile('/data/cortex/cortex.conf')

		# Set up logging to file
		if self.config['FILE_LOG'] == True:
			file_handler = RotatingFileHandler(self.config['LOG_DIR'] + '/' + self.config['LOG_FILE'], 'a', self.config['LOG_FILE_MAX_SIZE'], self.config['LOG_FILE_MAX_FILES'])
			file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
			self.logger.addHandler(file_handler)

		# Set up the max log level
		if self.debug:
			self.logger.setLevel(logging.DEBUG)
			file_handler.setLevel(logging.DEBUG)
		else:
			self.logger.setLevel(logging.INFO)
			file_handler.setLevel(logging.INFO)

		# Output some startup info
		self.logger.info('cortex version ' + self.config['VERSION_MAJOR'] + " r" + self.config['VERSION_MINOR'] + ' initialised')
		self.logger.info('cortex debug status: ' + str(self.config['DEBUG']))

		# set up e-mail alert logging
		if self.config['EMAIL_ALERTS'] == True:
			# Log to file where e-mail alerts are going to
			self.logger.info('cortex e-mail alerts are enabled and being sent to: ' + str(self.config['ADMINS']))

			# Create the mail handler
			mail_handler = SMTPHandler(self.config['SMTP_SERVER'], self.config['EMAIL_FROM'], self.config['ADMINS'], self.config['EMAIL_SUBJECT'])

			# Set the minimum log level (errors) and set a formatter
			mail_handler.setLevel(logging.ERROR)
			mail_handler.setFormatter(Formatter("""
A fatal error occured in Cortex.

Message type:       %(levelname)s
Location:           %(pathname)s:%(lineno)d
Module:             %(module)s
Function:           %(funcName)s
Time:               %(asctime)s
Logger Name:        %(name)s
Process ID:         %(process)d

Further Details:

%(message)s

"""))

			self.logger.addHandler(mail_handler)

		# Debug Toolbar
		if self.config['DEBUG_TOOLBAR']:
			self.debug = True
			from flask_debugtoolbar import DebugToolbarExtension
			toolbar = DebugToolbarExtension(app)
			self.logger.info('cortex debug toolbar enabled - DO NOT USE THIS ON LIVE SYSTEMS!')

		# check the database is up and is working
		self.init_database()

	################################################################################

	def pwgen(self, length=16):
		"""This is very crude password generator. It is currently only used to generate
		a CSRF token."""

		urandom = random.SystemRandom()
		alphabet = string.ascii_letters + string.digits
		return str().join(urandom.choice(alphabet) for _ in range(length))

	################################################################################

	def _generate_csrf_token(self):
		"""This function is used to generate a CSRF token for use in templates."""

		if '_csrf_token' not in session:
			session['_csrf_token'] = self.pwgen(32)

		return session['_csrf_token']

	################################################################################

	def _csrf_protect(self):
		"""Performs the checking of CSRF tokens. This check is skipped for the 
		GET, HEAD, OPTIONS and TRACE methods within HTTP, and is also skipped
		for any function that has been added to _exempt_views by use of the
		disable_csrf_check decorator."""

		# For methods that require CSRF checking
		if request.method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
			# Get the function that is rendering the current view
			view = self.view_functions.get(request.endpoint)
			view_location = view.__module__ + '.' + view.__name__

			# If the view is not exempt
			if not view_location in self._exempt_views:
				token = session.get('_csrf_token')
				if not token or token != request.form.get('_csrf_token'):
					if 'username' in session:
						self.logger.warning('CSRF protection alert: %s failed to present a valid POST token', session['username'])
					else:
			 			self.logger.warning('CSRF protection alert: a non-logged in user failed to present a valid POST token')

					# The user should not have accidentally triggered this so just throw a 400
					abort(400)
			else:
				self.logger.debug('View ' + view_location + ' is exempt from CSRF checks')

	################################################################################

	def disable_csrf_check(self, view):
		"""A decorator that can be used to exclude a view from CSRF validation.
		Example usage of disable_csrf_check might look something like this:
			@app.disable_csrf_check
			@app.route('/some_view')
			def some_view():
				return render_template('some_view.html')
		:param view: The view to be wrapped by the decorator.
		"""

		view_location = view.__module__ + '.' + view.__name__
		self._exempt_views.add(view_location)
		self.logger.debug('Added CSRF check exemption for ' + view_location)
		return view

	################################################################################

	def _load_workflow_settings(self, filename): 
		"""Extracts the settings from the given config file."""

		# Start a new module, which will be the context for parsing the config
		d = imp.new_module('config')
		d.__file__ = filename

		# Read the contents of the configuration file and execute it as a
		# Python script within the context of a new module
		with open(filename) as config_file:
			exec(compile(config_file.read(), filename, 'exec'), d.__dict__)

		# Extract the config options, which are those variables whose names are
		# entirely in uppercase
		new_config = {}
		for key in dir(d):
			if key.isupper():
				new_config[key] = getattr(d, key)

		return new_config

	################################################################################

	def load_workflows(self):
		"""Attempts to load the workflow config files from the workflows directory
		which is defined in app.config['WORKFLOWS_DIR']. Each config file is loaded
		and the display name stored"""

		# Where to store workflow settings
		self.wfsettings = {}

		# Ensure that we have a directory
		if not os.path.isdir(self.config['WORKFLOWS_DIR']):
			self.logger.error("The config option WORKFLOWS_DIR is not a directory!")
			return

		# List all entries in the directory and iterate over them
		entries = os.listdir(self.config['WORKFLOWS_DIR'])
		found = False
		for entry in entries:
			# Ignore the . and .. entries, along with any hidden files
			if entry.startswith('.'):
				continue

			# Get the fully qualified path of the file
			fqp = os.path.join(self.config['WORKFLOWS_DIR'], entry)

			# If it's a directory...
			if os.path.isdir(fqp):
				# This is or rather should be a workflow directory
				found = True
				views_file = os.path.join(fqp, "views.py")
				try:
					view_module = imp.load_source(entry, views_file)
					self.logger.info("Loaded workflow '" + entry + "' views module")
				except Exception as ex:
					self.logger.warn("Could not load workflow from file " + views_file + ": " + str(ex))
					continue

				# Load settings for this workflow if they exist ( settings files are optional )
				settings_file = os.path.join(fqp, "workflow.conf")
				if os.path.isfile(settings_file):
					try:
						self.wfsettings[entry] = self._load_workflow_settings(settings_file)
						self.logger.info("Loaded workflow '" + entry + "' config file")
					except Exception as ex:
						self.logger.warn("Could not load workflow config file " + settings_file + ": " + str(ex))
						continue

		# Warn if we didn't find any workflows
		if not found:
			self.logger.warn("The WORKFLOWS_DIR directory is empty, no workflows could be loaded!")

		# Set up template loading. Firstly build a list of FileSystemLoaders
		# that will process templates in each workflows' templates directory
		loader_data = {}
		for workflow in self.workflows:
			template_dir = os.path.join(self.config['WORKFLOWS_DIR'], workflow['name'], 'templates')
			loader_data[workflow['name']] = jinja2.FileSystemLoader(template_dir)

		# Create a ChoiceLoader, which by default will use the default 
		# template loader, and if that fails, uses a PrefixLoader which
		# can check the workflow template directories
		choice_loader = jinja2.ChoiceLoader(
		[
			self.jinja_loader,
			jinja2.PrefixLoader(loader_data, '::')
		])
		self.jinja_loader = choice_loader

	################################################################################
		
	def workflow_handler(self, workflow_name, workflow_title, workflow_order=999, workflow_type=WF_CREATE, workflow_desc=None, **options):
		"""This is a decorator function that is used in workflows to add a view
		function into Cortex for creating new 'things'. It performs the 
		function of Flask's @app.route but also adds the view function
		to a menu on the website to allow the workflow to be activated by the 
		user.

		Usage is as follows:

		@app.workflow_handler(__name__,"Title on menu", methods=['GET','POST'])

		:param workflow_name: the name of the workflow. This should always be __name__.
		:param workflow_title: the title of the workflow, as it appears in the list
		:param workflow_order: an integer hint as to the ordering of the workflow within the list. Defaults to 999.
		:param workflow_type: the type of workflow task. either app.WF_CREATE or app.WF_SYSTEM_ACTION. Defaults to WF_CREATE.
		:param workflow_desc: a description of what this workflow view does. This is currently only used for WF_SYSTEM_ACTION. Defaults to None.
		:param options: the options to be forwarded to the underlying
			     :class:`~werkzeug.routing.Rule` object.  A change
			     to Werkzeug is handling of method options.  methods
			     is a list of methods this rule should be limited
			     to (``GET``, ``POST`` etc.).  By default a rule
			     just listens for ``GET`` (and implicitly ``HEAD``).
			     Starting with Flask 0.6, ``OPTIONS`` is implicitly
			     added and handled by the standard request handling.
		"""

		def decorator(f):

			if workflow_type == self.WF_CREATE:
				rule = "/workflows/" + f.__name__
			elif workflow_type == self.WF_SYSTEM_ACTION:
				rule = "/workflows/" + f.__name__ + "/<int:id>"
			else:
				app.logger.warn("Workflow '" + workflow_name + " could not be loaded because the workflow_type is invalid")
				return


			# Get the endpoint
			endpoint = options.pop('endpoint', None)

			# This is what Flask normally does for a route, which allows the
			# page to be accessible
			self.add_url_rule(rule, endpoint, f, **options)

			# Store the workflow details in a hash
			wfdata = {'display': workflow_title, 'name': workflow_name, 'order': workflow_order, 'view_func': f.__name__, 'description': workflow_desc }

			if workflow_type == self.WF_CREATE:
				self.logger.info("Registered workflow creation view '" + f.__name__ + "'")
				self.workflows.append(wfdata)
			elif workflow_type == self.WF_SYSTEM_ACTION:
				self.logger.info("Registered workflow system action view '" + f.__name__ + "'")
				self.system_actions.append(wfdata)

			return f

		return decorator

	################################################################################

	def log_exception(self, exc_info):
		"""Logs an exception.  This is called by :meth:`handle_exception`
		if debugging is disabled and right before the handler is called.
		This implementation logs the exception as an error on the
		:attr:`logger` but sends extra information such as the remote IP
		address, the username, etc. This extends the default implementation
		in Flask.

		"""

		if 'username' in session:
			usr = session['username']
		else:
			usr = 'Not logged in'

		self.logger.error("""Path:                 %s 
HTTP Method:          %s
Client IP Address:    %s
User Agent:           %s
User Platform:        %s
User Browser:         %s
User Browser Version: %s
Username:             %s
""" % (
			request.path,
			request.method,
			request.remote_addr,
			request.user_agent.string,
			request.user_agent.platform,
			request.user_agent.browser,
			request.user_agent.version,
			usr,
			
		), exc_info=exc_info)

################################################################################

	def init_database(self):
		"""Ensures cortex can talk to the database (rather than waiting for a HTTP
		connection to trigger before_request) and the tables are there. Only runs
		at cortex startup."""

		# Connect to database
		try:
			temp_db = mysql.connect(host=self.config['MYSQL_HOST'], port=self.config['MYSQL_PORT'], user=self.config['MYSQL_USER'], passwd=self.config['MYSQL_PASS'], db=self.config['MYSQL_NAME'])
		except Exception as ex:
			raise Exception("Could not connect to MySQL server: " + str(type(ex)) + " - " + str(ex))

		self.logger.info("Successfully connected to the MySQL database server")

		## Now create tables if they don't exist
		cursor = temp_db.cursor()

		## Turn on autocommit so each table is created in sequence
		cursor.connection.autocommit(True)

		## Turn off warnings (MySQLdb generates warnings even though we use IF NOT EXISTS- wtf?!)
		cursor._defer_warnings = True

		self.logger.info("Checking for and creating tables as necessary")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `classes` (`name` varchar(16) NOT NULL,
		  `digits` tinyint(4) NOT NULL,
		  `disabled` tinyint(1) NOT NULL DEFAULT '0',
		  `lastid` int(11) DEFAULT '0',
		  `comment` text,
		  `link_vmware` tinyint(1) NOT NULL DEFAULT '0',
		  `cmdb_type` varchar(64) DEFAULT NULL,
		  PRIMARY KEY (`name`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `events` (
		  `id` mediumint(11) NOT NULL AUTO_INCREMENT,
		  `source` varchar(255) NOT NULL,
		  `related_id` mediumint(11) DEFAULT NULL,
		  `name` varchar(255) NOT NULL,
		  `username` varchar(64) NOT NULL,
		  `desc` text,
		  `status` tinyint(4) NOT NULL DEFAULT '0',
		  `start` datetime NOT NULL,
		  `end` datetime DEFAULT NULL,
		  PRIMARY KEY (`id`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `kv_settings` (
		  `key` varchar(64) NOT NULL,
		  `value` text,
		  PRIMARY KEY (`key`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `systems` (
		  `id` mediumint(11) NOT NULL AUTO_INCREMENT,
		  `type` tinyint(4) NOT NULL,
		  `class` varchar(16) DEFAULT NULL,
		  `number` mediumint(11) DEFAULT NULL,
		  `name` varchar(256) NOT NULL,
		  `allocation_date` datetime DEFAULT NULL,
		  `allocation_who` varchar(64) DEFAULT NULL,
		  `allocation_comment` text NOT NULL,
		  `cmdb_id` varchar(128) DEFAULT NULL,
		  `vmware_uuid` varchar(36) DEFAULT NULL,
		  `review_status` tinyint(4) NOT NULL DEFAULT 0,
		  `review_task` varchar(16) DEFAULT NULL,
		  `expiry_date` datetime DEFAULT NULL,
		  PRIMARY KEY (`id`),
		  KEY `class` (`class`),
		  KEY `name` (`name`(255)),
		  KEY `allocation_who` (`allocation_who`),
		  CONSTRAINT `systems_ibfk_1` FOREIGN KEY (`class`) REFERENCES `classes` (`name`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `tasks` (
		  `id` mediumint(11) NOT NULL AUTO_INCREMENT,
		  `module` varchar(64) NOT NULL,
		  `username` varchar(64) NOT NULL,
		  `start` datetime NOT NULL,
		  `end` datetime DEFAULT NULL,
		  `status` tinyint(4) NOT NULL DEFAULT '0',
		  `description` text,
		  PRIMARY KEY (`id`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS  `puppet_groups` (
		  `name` varchar(255) NOT NULL,
		  `classes` text NOT NULL,
		  PRIMARY KEY (`name`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `puppet_nodes` (
		  `id` mediumint(11) NOT NULL,
		  `certname` varchar(255) NOT NULL,
		  `env` varchar(255) NOT NULL DEFAULT 'production',
		  `include_default` tinyint(1) NOT NULL DEFAULT '1',
		  `classes` text NOT NULL,
		  `variables` text NOT NULL,
		  PRIMARY KEY (`id`),
		  CONSTRAINT `puppet_nodes_ibfk_1` FOREIGN KEY (`id`) REFERENCES `systems` (`id`) ON DELETE CASCADE
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `sncache_cmdb_ci` (
		  `sys_id` varchar(32) NOT NULL,
		  `sys_class_name` varchar(128) NOT NULL,
		  `name` varchar(255) NOT NULL,
		  `operational_status` varchar(255) NOT NULL,
		  `u_number` varchar(255) NOT NULL,
		  `short_description` text NOT NULL,
		  `u_environment` varchar(255) DEFAULT NULL,
		  `virtual` tinyint(1) NOT NULL,
		  `comments` text,
		  `os` varchar(128) DEFAULT NULL,
		  PRIMARY KEY (`sys_id`),
		  KEY `u_number` (`u_number`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `vmware_cache_clusters` (
		  `id` varchar(255) NOT NULL,
		  `name` varchar(255) NOT NULL,
		  `vcenter` varchar(255) NOT NULL,
		  `did` varchar(255) DEFAULT NULL,
		  `ram` bigint DEFAULT 0,
		  `cores` int DEFAULT 0,
		  `cpuhz` bigint DEFAULT 0,
		  `ram_usage` bigint DEFAULT 0,
		  `cpu_usage` bigint DEFAULT 0,
		  `hosts` bigint(20) DEFAULT '0',
		  PRIMARY KEY (`id`,`vcenter`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `vmware_cache_datacenters` (
		  `id` varchar(255) NOT NULL,
		  `name` varchar(255) NOT NULL,
		  `vcenter` varchar(255) NOT NULL,
		  PRIMARY KEY (`id`,`vcenter`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `vmware_cache_vm` (
		  `id` varchar(255) NOT NULL,
		  `vcenter` varchar(255) NOT NULL,
		  `name` varchar(255) NOT NULL,
		  `uuid` varchar(36) NOT NULL,
		  `numCPU` int(11) NOT NULL,
		  `memoryMB` int(11) NOT NULL,
		  `powerState` varchar(255) NOT NULL,
		  `guestFullName` varchar(255) NOT NULL,
		  `guestId` varchar(255) NOT NULL,
		  `hwVersion` varchar(255) NOT NULL,
		  `hostname` varchar(255) NOT NULL,
		  `ipaddr` varchar(255) NOT NULL,
		  `annotation` text,
		  `cluster` varchar(255) NOT NULL,
		  `toolsRunningStatus` varchar(255) NOT NULL,
		  `toolsVersionStatus` varchar(255) NOT NULL,
		  `template` tinyint(1) NOT NULL DEFAULT '0',
		  PRIMARY KEY (`id`,`vcenter`),
		  KEY `uuid` (`uuid`),
		  KEY `name` (`name`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8;""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `vmware_cache_folders` (
		  `id` varchar(255) NOT NULL,
		  `name` varchar(255) NOT NULL,
		  `vcenter` varchar(255) NOT NULL,
		  `did` varchar(255) NOT NULL,
		  `parent` varchar(255) NOT NULL,
		  PRIMARY KEY (`id`,`vcenter`,`did`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `stats_vm_count` (
		  `timestamp` DATETIME NOT NULL,
		  `value` mediumint(11) NOT NULL,
		  PRIMARY KEY (`timestamp`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `stats_linux_vm_count` (
		  `timestamp` DATETIME NOT NULL,
		  `value` mediumint(11) NOT NULL,
		  PRIMARY KEY (`timestamp`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `stats_windows_vm_count` (
		  `timestamp` DATETIME NOT NULL,
		  `value` mediumint(11) NOT NULL,
		  PRIMARY KEY (`timestamp`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `stats_desktop_vm_count` (
		  `timestamp` DATETIME NOT NULL,
		  `value` mediumint(11) NOT NULL,
		  PRIMARY KEY (`timestamp`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `stats_other_vm_count` (
		  `timestamp` DATETIME NOT NULL,
		  `value` mediumint(11) NOT NULL,
		  PRIMARY KEY (`timestamp`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8""")

		cursor.execute("""CREATE TABLE IF NOT EXISTS `realname_cache` (
		 `username` varchar(64) NOT NULL,
		 `realname` varchar(255),
		 PRIMARY KEY (`username`)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8""")

		try:
			cursor.execute("""ALTER TABLE `systems` ADD `expiry_date` datetime DEFAULT NULL""")
		except Exception, e:
			pass

		cursor.execute("""CREATE OR REPLACE VIEW `systems_info_view` AS SELECT `systems`.`id` AS `id`, `type`, `class`, `number`, `systems`.`name` AS `name`, `allocation_date`, `expiry_date`, `allocation_who`, `allocation_comment`, `cmdb_id`, `sys_class_name` AS `cmdb_sys_class_name`, `sncache_cmdb_ci`.`name` AS `cmdb_name`, `operational_status` AS `cmdb_operational_status`, `u_number` AS `cmdb_u_number`, `sncache_cmdb_ci`.`short_description` AS `cmdb_short_description`, `vmware_cache_vm`.`name` AS `vmware_name`, `vmware_cache_vm`.`vcenter` AS `vmware_vcenter`, `vmware_cache_vm`.`uuid` AS `vmware_uuid`, `vmware_cache_vm`.`numCPU` AS `vmware_cpus`, `vmware_cache_vm`.`memoryMB` AS `vmware_ram`, `vmware_cache_vm`.`powerState` AS `vmware_guest_state`, `vmware_cache_vm`.`guestFullName` AS `vmware_os`, `vmware_cache_vm`.`hwVersion` AS `vmware_hwversion`, `vmware_cache_vm`.`ipaddr` AS `vmware_ipaddr`, `vmware_cache_vm`.`toolsVersionStatus` AS `vmware_tools_version_status`, `puppet_nodes`.`certname` AS `puppet_certname`, `puppet_nodes`.`env` AS `puppet_env`, `puppet_nodes`.`include_default` AS `puppet_include_default`, `puppet_nodes`.`classes` AS `puppet_classes`, `puppet_nodes`.`variables` AS `puppet_variables`, `sncache_cmdb_ci`.`u_environment` AS `cmdb_environment`, `sncache_cmdb_ci`.`short_description` AS `cmdb_description`, `sncache_cmdb_ci`.`comments` AS `cmdb_comments`, `sncache_cmdb_ci`.`os` AS `cmdb_os`, `vmware_cache_vm`.`hostname` AS `vmware_hostname`, `systems`.`review_status` AS `review_status`, `systems`.`review_task` AS `review_task`, `realname_cache`.`realname` AS `allocation_who_realname` FROM `systems` LEFT JOIN `sncache_cmdb_ci` ON `systems`.`cmdb_id` = `sncache_cmdb_ci`.`sys_id` LEFT JOIN `vmware_cache_vm` ON `systems`.`vmware_uuid` = `vmware_cache_vm`.`uuid` LEFT JOIN `puppet_nodes` ON `systems`.`id` = `puppet_nodes`.`id` LEFT JOIN `realname_cache` ON `systems`.`allocation_who` = `realname_cache`.`username`""")
       	
		## Close database connection
		temp_db.close()

		self.logger.info("Database initialisation complete")
