from cortex import app
import cortex.lib.dsc
import Pyro4.errors
from cortex.lib.user import does_user_have_permission, does_user_have_system_permission, does_user_have_any_system_permission, is_system_enrolled
from cortex.corpus import Corpus
from flask import Flask, request, session, redirect, url_for, flash, g, abort, make_response, render_template, jsonify, Response
import MySQLdb as mysql
import json
import yaml
import os

class Role:
	"""docstring for Role"""
	def __init__(self, full_name, short_name, prefix, contents):
		self.full_name = full_name
		self.short_name = short_name
		self.contents = contents
		self.prefix = prefix
		self.trimmed_prefix = self.prefix.replace("UOS", "")

	def serialize(self):
		return {
			'full_name': self.full_name,
			'short_name': self.short_name,
			'contents': self.contents,
			'prefix': self.prefix,
			'trimmed_prefix': self.trimmed_prefix
		}

	def __repr__(self):
		return str({
			'full_name': self.full_name,
			'short_name': self.short_name,
			'prefix': self.prefix,
			'trimmed_prefix': self.trimmed_prefix
		})

def yamlload(s):
	return yaml.load(s, Loader=yaml.FullLoader)

@app.route('/dsc/classify/<id>', methods=['GET', 'POST'])
@cortex.lib.user.login_required
def dsc_classify_machine(id):
	system = cortex.lib.systems.get_system_by_id(id)

	if system == None:
		abort(404)
	
	curd = g.db.cursor(mysql.cursors.DictCursor)
	page_cont = {}

	# get a proxy to connect to dsc

	
	# Setting up cache
	# If loading for remote work, set this to true once and then false
	roles_info = {}
	if False:
		dsc_proxy = cortex.lib.dsc.dsc_connect()
		roles_info = cortex.lib.dsc.get_roles(dsc_proxy)
		with open('/srv/cortex/dsc_cache.txt', 'w+') as f:
			f.write(json.dumps(roles_info))
	else:
		with open('/srv/cortex/dsc_cache.txt') as f:
			fdata = f.read()
			# print(fdata)
			roles_info = json.loads(fdata)

	role_obj = []
	for role in roles_info:
		if role == "AllNodes": continue
		n = role
		role_obj.append(Role(role, n.split("_")[1], n.split("_")[0], roles_info[role]))


	default_roles = [role for role in roles_info.keys()]
	# generates set of jobs
	# removes UOS from the job and takes the part of the string before the '_'
	# if the job is generic or allnodes (the special 2 that we don't need as they're applied to every box), it ignores it 
	jobs = list({((r.replace('UOS', '')).split('_'))[0] for r in default_roles if not any(special_role in r for special_role in ['AllNodes', 'Generic'])})

	role_selections = {}
	for job in jobs + ['Generic']:
		role_selections[job] = [r for r in default_roles if job in r]
	
	page_cont['system'] = system = cortex.lib.systems.get_system_by_id(id)

		# retrieve all the systems
	curd.execute("SELECT `roles`, `config` FROM `dsc_config` WHERE `system_id` = %s", (id, ))
	existing_data = curd.fetchone()

	exist_role = existing_data['roles']
	exist_config = existing_data['config']

	# get existing info
	print(repr(exist_config))
	if exist_config == "\"\"" or exist_config == 'null\n...\n':
		print('truew')
		base_role = {'AllNodes': roles_info['AllNodes']}
		exist_config = base_role
	else:
		exist_config = yamlload(exist_config)


	page_cont['tickvalues'] = list(yamlload(exist_role))
	page_cont['roles'] = jobs
	print(type(exist_config))
	page_cont['yaml_config'] = yaml.dump(exist_config)

	if request.method == "POST":

		selected_vals = request.form['selected_values'].split(",")
		if selected_vals == ['']:
			selected_vals = []


		to_add = list(set(selected_vals) - set(page_cont['tickvalues']))
		to_del = list(set(page_cont['tickvalues']) - set(selected_vals))

		print(to_add, to_del)
		configs = ""
		try:
			configs = yamlload(request.form['configurations'])
		except Exception as e:
			print(e)
			flash('Error in Configuration', 'alert-danger')
			configs = exist_config

		# Add the roles into the config
		config_roles = selected_vals
		config_roles = config_roles + ['Generic']

		configs['AllNodes'][1]['Role']= [x.full_name for x in role_obj if x.trimmed_prefix in config_roles]

		print(configs)
		curd.execute('UPDATE `dsc_config` SET config = %s, roles = %s WHERE system_id = %s;', (yaml.dump(configs), yaml.dump(selected_vals), id))
		g.db.commit()
		# return jsonify([configs, selected_vals])

	elif request.method == "GET":
		pass
	#return jsonify(page_cont['roles'])
	#return jsonify(page_cont)
	return render_template('dsc/classify.html', title="DSC", page_cont=page_cont, system=system)
	return render_template('dsc/classify.html', title="DSC", system=system, active='dsc', roles=jobs, yaml=exist_config, set_roles=exist_role, role_info=roles_info, yaml_role_info=yaml_roles_info, selectpicker_tick=list(values_to_tick))	




