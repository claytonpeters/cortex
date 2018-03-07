#!/usr/bin/python

from cortex import app
from cortex.lib.workflow import CortexWorkflow
import cortex.lib.core
import cortex.lib.systems
from cortex.corpus import Corpus
from flask import Flask, request, session, redirect, url_for, flash, g, abort, render_template
from pyVmomi import vim
from itsdangerous import JSONWebSignatureSerializer
import requests
import ldap

workflow = CortexWorkflow(__name__)
workflow.add_permission('systems.all.decom', 'Decommission any system')
workflow.add_system_permission('decom', 'Decommission system')



@workflow.action("prepare",title='Decommission', desc="Begins the process of decommissioning this system", system_permission="decom", permission="systems.all.decom")
def decom_step1(id):
	system = cortex.lib.systems.get_system_by_id(id)
	if system is None:
		abort(404)

	return workflow.render_template("step1.html", system=system, title="Decommission system")

@workflow.action("check",title='Decomission', system_permission="decom", permission="systems.all.decom",menu=False)
def decom_step2(id):
	# in this step we work out what steps to perform
	# then we load this into a list of steps, each step being a dictionary
	# this is used on the page to list the steps to the user
	# the list is also used to generate a JSON document which we sign using
	# app.config['SECRET_KEY'] and then send that onto the page as well.

	# load the corpus library
	corpus = Corpus(g.db,app.config)

	system = cortex.lib.systems.get_system_by_id(id)
	if system is None:
		abort(404)

	actions = []

	systemenv = None
	## Find the environment that this VM is in based off of the CMDB env
	if 'cmdb_environment' in system:
		if system['cmdb_environment'] is not None:
			for env in app.config['ENVIRONMENTS']:
				if env['name'] == system['cmdb_environment']:
					# We found the environment matching the system
					systemenv = env
					break


	## Is the system linked to vmware?
	if 'vmware_uuid' in system:
		if system['vmware_uuid'] is not None:
			if len(system['vmware_uuid']) > 0:
				## The system is linked to vmware - e.g. a VM exists

				vmobj = corpus.vmware_get_vm_by_uuid(system['vmware_uuid'],system['vmware_vcenter'])

				if vmobj:
					if vmobj.runtime.powerState == vim.VirtualMachine.PowerState.poweredOn:
						actions.append({'id': 'vm.poweroff', 'desc': 'Power off the virtual machine ' + system['name'], 'detail': 'UUID ' + system['vmware_uuid'] + ' on ' + system['vmware_vcenter'], 'data': {'uuid': system['vmware_uuid'], 'vcenter': system['vmware_vcenter']}})

					actions.append({'id': 'vm.delete', 'desc': 'Delete the virtual machine ' + system['name'], 'detail': ' UUID ' + system['vmware_uuid'] + ' on ' + system['vmware_vcenter'], 'data': {'uuid': system['vmware_uuid'], 'vcenter': system['vmware_vcenter']}})

	## Is the system linked to service now?
	if 'cmdb_id' in system:
		if system['cmdb_id'] is not None:
			if len(system['cmdb_id']) > 0:
				# Add a task to mark the object as Deleted/Decommissioned
				if system['cmdb_is_virtual']:
					if system['cmdb_operational_status'] != u'Deleted':
						actions.append({'id': 'cmdb.update', 'desc': 'Mark the system as Deleted in the CMDB', 'detail': system['cmdb_id'] + " on " + app.config['SN_HOST'], 'data': system['cmdb_id']})
				else:
					if system['cmdb_operational_status'] != u'Decommissioned':
						actions.append({'id': 'cmdb.update', 'desc': 'Mark the system as Decommissioned in the CMDB', 'detail': system['cmdb_id'] + " on " + app.config['SN_HOST'], 'data': system['cmdb_id']})

				# Remove CI relationships if the exist
				try:
					rel_count = len(corpus.servicenow_get_ci_relationships(system['cmdb_id']))
					if rel_count > 0:
						actions.append({'id': 'cmdb.relationships.delete', 'desc': 'Remove ' + str(rel_count) + ' relationships from the CMDB CI', 'detail': str(rel_count) + ' entries from ' + system['cmdb_id'] + " on " + app.config['SN_HOST'], 'data': system['cmdb_id']})
				except Exception as ex:
					flash('Warning - An error occured when communicating with ServiceNow: ' + str(ex), 'alert-warning')


	## Ask Infoblox if a DNS host object exists for the name of the system
	try:
		refs = corpus.infoblox_get_host_refs(system['name'] + ".soton.ac.uk")

		if refs is not None:
			for ref in refs:
				actions.append({'id': 'dns.delete', 'desc': 'Delete the DNS record ' + ref.split(':')[-1], 'detail': 'Delete the name ' + system['name'] + '.soton.ac.uk - Infoblox reference: ' + ref, 'data': ref})

	except Exception as ex:
		flash("Warning - An error occured when communicating with Infoblox: " + str(type(ex)) + " - " + str(ex),"alert-warning")

	## Check if a puppet record exists
	if 'puppet_certname' in system:
		if system['puppet_certname'] is not None:
			if len(system['puppet_certname']) > 0:
				actions.append({'id': 'puppet.cortex.delete', 'desc': 'Delete the Puppet ENC configuration', 'detail': system['puppet_certname'] + ' on ' + request.url_root, 'data': system['id']})
				actions.append({'id': 'puppet.master.delete', 'desc': 'Delete the system from the Puppet Master', 'detail': system['puppet_certname'] + ' on ' + app.config['PUPPET_MASTER'], 'data': system['puppet_certname']})

	## Check if TSM backups exist
	try:
		tsm_clients = corpus.tsm_get_system(system['name'])
		#if the TSM client is not decomissioned, then decomission it
		for client in tsm_clients:
			if client['DECOMMISSIONED'] is None:
				actions.append({'id': 'tsm.decom', 'desc': 'Decommission the system in TSM', 'detail': 'Node ' + client['NAME']  + ' on server ' + client['SERVER'], 'data': {'NAME': client['NAME'], 'SERVER': client['SERVER']}})
	except requests.exceptions.HTTPError as e:
		flash("Warning - An error occured when communicating with TSM: " + str(ex), "alert-warning")
	except LookupError:
		pass
	except Exception as ex:
		flash("Warning - An error occured when communicating with TSM: " + str(ex), "alert-warning")

	# We need to check all (unique) AD domains as we register development
	# Linux boxes to the production domain
	tested_domains = set()
	for adenv in app.config['WINRPC']:
		try:
			# If we've not tested this CortexWindowsRPC host before
			if app.config['WINRPC'][adenv]['host'] not in tested_domains:
				# Add it to the set of tested hosts
				tested_domains.update([app.config['WINRPC'][adenv]['host']])

				# If an AD object exists, append an action to delete it from that environment
				if corpus.windows_computer_object_exists(adenv, system['name']):
					actions.append({'id': 'ad.delete', 'desc': 'Delete the Active Directory computer object', 'detail': system['name'] + ' on domain ' + app.config['WINRPC'][adenv]['domain'], 'data': {'hostname': system['name'], 'env': adenv}})

		except Exception as ex:
			flash("Warning - An error occured when communicating with Active Directory: " + str(type(ex)) + " - " + str(ex), "alert-warning")

	## Work out the URL for any RHN systems
	rhnurl = app.config['RHN5_URL']
	if not rhnurl.endswith("/"):
		rhnurl = rhnurl + "/"
	rhnurl = rhnurl + "rhn/systems/details/Overview.do?sid="

	## Check if a record exists in RHN for this system
	try:
		(rhn,key) = corpus.rhn5_connect()
		rsystems = rhn.system.search.hostname(key,system['name'])
		if len(rsystems) > 0:
			for rsys in rsystems:
				actions.append({'id': 'rhn5.delete', 'desc': 'Delete the RHN Satellite object', 'detail': rsys['name'] + ', RHN ID <a target="_blank" href="' + rhnurl + str(rsys['id']) + '">' + str(rsys['id']) + "</a>", 'data': {'id': rsys['id']}})
	except Exception as ex:
		flash("Warning - An error occured when communicating with RHN: " + str(ex), "alert-warning")

	## Check sudoldap for sudoHost entries
	try:
		# Connect to LDAP
		l = ldap.initialize(workflow.config['SUDO_LDAP_URL'])
		l.bind_s(workflow.config['SUDO_LDAP_USER'], workflow.config['SUDO_LDAP_PASS'])

		# This contains our list of changes and keeps track of sudoHost entries
		ldap_dn_data = {}

		# Iterate over the search domains
		for domain_suffix in workflow.config['SUDO_LDAP_SEARCH_DOMAINS']:
			# Prefix '.' to our domain suffix if necessary
			if domain_suffix != '' and domain_suffix[0] != '.':
				domain_suffix = '.' + domain_suffix

			# Get our host entry
			host = system['name'] + domain_suffix

			formatted_filter = workflow.config['SUDO_LDAP_FILTER'].format(host)
			results = l.search_s(workflow.config['SUDO_LDAP_SEARCH_BASE'], ldap.SCOPE_SUBTREE, formatted_filter)

			for result in results:
				dn = result[0]

				# Store the sudoHosts for each DN we find
				if dn not in ldap_dn_data:
					ldap_dn_data[dn] = {'cn': result[1]['cn'][0], 'sudoHost': result[1]['sudoHost'], 'action': 'none', 'count': 0, 'remove': []}

				# Keep track of what things will look like after a deletion (so
				# we can track when a sudoHosts entry becomes empty and as such
				# the entry should be deleted)
				for idx, entry in enumerate(ldap_dn_data[dn]['sudoHost']):
					if entry.lower() == host.lower():
						ldap_dn_data[dn]['sudoHost'].pop(idx)
						ldap_dn_data[dn]['action'] = 'modify'
						ldap_dn_data[dn]['remove'].append(entry)

		# Determine if any of the DNs are now empty
		for dn in ldap_dn_data:
			if len(ldap_dn_data[dn]['sudoHost']) == 0:
				ldap_dn_data[dn]['action'] = 'delete'

		# Print out actions
		for dn in ldap_dn_data:
			if ldap_dn_data[dn]['action'] == 'modify':
				for entry in ldap_dn_data[dn]['remove']:
					actions.append({'id': 'sudoldap.update', 'desc': 'Remove sudoHost attribute value ' + entry + ' from ' + ldap_dn_data[dn]['cn'], 'detail': 'Update object ' + dn + ' on ' + workflow.config['SUDO_LDAP_URL'], 'data': {'dn': dn, 'value': entry}})
			elif ldap_dn_data[dn]['action'] == 'delete':
				actions.append({'id': 'sudoldap.delete', 'desc': 'Delete ' + ldap_dn_data[dn]['cn'] + ' because we\'ve removed its last sudoHost attribute', 'detail': 'Delete ' + dn + ' on ' + workflow.config['SUDO_LDAP_URL'], 'data': {'dn': dn, 'value': ldap_dn_data[dn]['sudoHost']}})
			
	except Exception as ex:
		flash('Warning - An error occurred when communication with ' + str(workflow.config['SUDO_LDAP_URL']) + ': ' + str(ex), 'alert-warning')

	# If the config says nothing about creating a ticket, or the config 
	# says to create a ticket:
	if 'TICKET_CREATE' not in workflow.config or workflow.config['TICKET_CREATE'] is True:
		# If there are actions to be performed, add on an action to raise a ticket to ESM (but not for Sandbox!)
		if len(actions) > 0 and system['class'] != "play":
			actions.append({'id': 'ticket.ops', 'desc': 'Raises a ticket with operations to perform manual steps, such as removal from monitoring', 'detail': 'Creates a ticket in ServiceNow and assigns it to ' + workflow.config['TICKET_TEAM'], 'data': {'hostname': system['name']}})

	# Turn the actions list into a signed JSON document via itsdangerous
	signer = JSONWebSignatureSerializer(app.config['SECRET_KEY'])
	json_data = signer.dumps(actions)

	return workflow.render_template("step2.html", actions=actions, system=system, json_data=json_data, title="Decommission Node")

@workflow.action("start",title='Decomission', system_permission="decom", permission="systems.all.decom", menu=False, methods=['POST'])
def decom_step3(id):
	## Get the actions list 
	actions_data = request.form['actions']

	## Decode it 
	signer = JSONWebSignatureSerializer(app.config['SECRET_KEY'])
	try:
		actions = signer.loads(actions_data)
	except itsdangerous.BadSignature as ex:
		abort(400)

	# Build the options to send on to the task
	options = {'actions': []}
	if request.form.get("runaction", None) is not None:
		for action in request.form.getlist("runaction"):
			options['actions'].append(actions[int(action)])
	options['wfconfig'] = workflow.config

	# Connect to NeoCortex and start the task
	neocortex = cortex.lib.core.neocortex_connect()
	task_id = neocortex.create_task(__name__, session['username'], options, description="Decommissions a system")

	# Redirect to the status page for the task
	return redirect(url_for('task_status', id=task_id))
