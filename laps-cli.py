#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
from os import path, makedirs, rename
from datetime import datetime
from dns import resolver, rdatatype
import ldap3
import ssl
import getpass
import argparse
import json
import sys
import os


# Microsoft Timestamp Conversion
EPOCH_TIMESTAMP = 11644473600  # January 1, 1970 as MS file time
HUNDREDS_OF_NANOSECONDS = 10000000
def dt_to_filetime(dt): # dt.timestamp() returns UTC time as expected by the LDAP server
	return int((dt.timestamp() + EPOCH_TIMESTAMP) * HUNDREDS_OF_NANOSECONDS)
def filetime_to_dt(ft): # ft is in UTC, fromtimestamp() converts to local time
	return datetime.fromtimestamp(int((ft / HUNDREDS_OF_NANOSECONDS) - EPOCH_TIMESTAMP))


class LapsCli():
	PLATFORM          = sys.platform.lower()

	PRODUCT_NAME      = 'LAPS4LINUX CLI'
	PRODUCT_VERSION   = '1.6.3'
	PRODUCT_WEBSITE   = 'https://github.com/schorschii/laps4linux'

	gcModeOn    = False
	server      = None
	connection  = None
	tmpDn       = ''

	tlsSettings = ldap3.Tls(validate=ssl.CERT_REQUIRED)

	cfgPresetDirWindows = sys.path[0]
	cfgPresetDirUnix    = '/etc'
	cfgPresetFile       = 'laps-client.json'
	cfgPresetPath       = (cfgPresetDirWindows if sys.platform.lower()=='win32' else cfgPresetDirUnix)+'/'+cfgPresetFile 

	cfgDir      = str(Path.home())+'/.config/laps-client'
	cfgPath     = cfgDir+'/settings.json'
	cfgPathOld  = str(Path.home())+'/.laps-client.json'
	cfgUseKerberos = True
	cfgUseStartTls = True
	cfgServer   = []
	cfgDomain   = None
	cfgUsername = ''
	cfgPassword = ''
	cfgLdapAttributes              = {
		'Administrator Password':           'msLAPS-Password',
		'Decrypted Administrator Password': 'msLAPS-EncryptedPassword',
		'Password Expiration Date':         'msLAPS-PasswordExpirationTime',
		'Administrator Password History':   'msLAPS-EncryptedPasswordHistory'
	}
	cfgLdapAttributePassword        = 'msLAPS-EncryptedPassword'
	cfgLdapAttributePasswordExpiry  = 'msLAPS-PasswordExpirationTime'
	cfgLdapAttributePasswordHistory = 'msLAPS-EncryptedPasswordHistory'


	def __init__(self, useKerberos=None):
		self.LoadSettings()
		if(useKerberos != None): self.cfgUseKerberos = useKerberos

		# show version information
		print(self.PRODUCT_NAME+' v'+self.PRODUCT_VERSION)
		print('If you like LAPS4LINUX please do not forget to give the repository a star ('+self.PRODUCT_WEBSITE+').')

	def GetAttributesAsDict(self):
		finalDict = {}
		if(isinstance(self.cfgLdapAttributes, list)):
			for attribute in self.cfgLdapAttributes:
				finalDict[attribute] = attribute
		elif(isinstance(self.cfgLdapAttributes, dict)):
			for title, attribute in self.cfgLdapAttributes.items():
				finalDict[str(title)] = str(attribute)
		return finalDict

	def SearchComputer(self, computerName):
		# check and escape input
		if computerName.strip() == '': return
		if not computerName == '*': computerName = ldap3.utils.conv.escape_filter_chars(computerName)

		# ask for credentials and print connection details
		print('')
		if not self.checkCredentialsAndConnect(): return
		self.printResult('Connection', self.GetConnectionString())

		try:
			# compile query attributes
			attributes = ['SAMAccountname', 'distinguishedName']
			for title, attribute in self.GetAttributesAsDict().items():
				attributes.append(str(attribute))
			# start LDAP search
			count = 0
			self.connection.search(
				search_base=self.createLdapBase(self.connection),
				search_filter='(&(objectCategory=computer)(name='+computerName+'))',
				attributes=attributes
			)
			for entry in self.connection.entries:
				count += 1
				# display result list
				if computerName == '*':
					displayValues = [str(entry['SAMAccountname'])]
					for title, attribute in self.GetAttributesAsDict().items():
						stringValue = str(entry[str(attribute)])
						try:
							# decrypt if necessary
							if(len(entry[str(attribute)]) > 0 and type(entry[str(attribute)][0]) is bytes):
								decryptedValue = self.decryptPassword(entry[str(attribute)][0][16:])
								if(decryptedValue): stringValue = decryptedValue
							# parse Native LAPS JSON
							jsonValue = json.loads(stringValue)
							if(not 'n' in jsonValue or not 'p' in jsonValue or not 't' in jsonValue):
								raise Exception('Invalid LAPS JSON')
							# display values
							displayValues.append(jsonValue['p']+'  ('+jsonValue['n']+')  ('+str(filetime_to_dt( int(jsonValue['t'], 16)) )+')')
						except Exception as e:
							displayValues.append(stringValue)
					print("\t".join(displayValues))

				# display single result
				else:
					self.printResult('Found', str(entry['distinguishedName']))
					self.tmpDn = str(entry['distinguishedName'])
					self.queryAttributes()
					return

			# no result found
			if count == 0: eprint('No result for query »'+computerName+'«')
		except Exception as e:
			# display error
			eprint('Error:', str(e))
			# reset connection
			self.server = None
			self.connection = None

		self.tmpDn = ''

	def SetExpiry(self, newExpirationDateTimeString):
		# check if dn of target computer object is known
		if self.tmpDn.strip() == '': return

		try:
			# calc new time
			newExpirationDate = datetime.strptime(newExpirationDateTimeString, '%Y-%m-%d %H:%M:%S')
			newExpirationDateTime = dt_to_filetime( newExpirationDate )
			self.printResult('New Expiration', str(newExpirationDateTime)+' ('+str(newExpirationDate)+')')

			# start LDAP modify
			self.connection.modify(self.tmpDn, { self.cfgLdapAttributePasswordExpiry: [(ldap3.MODIFY_REPLACE, [str(newExpirationDateTime)])] })
			if self.connection.result['result'] == 0:
				print('Expiration Date Changed Successfully.')
			else:
				print('Unable to change expiration date. '+str(self.connection.result['message']))

		except Exception as e:
			# display error
			eprint('Error:', str(e))
			# reset connection
			self.server = None
			self.connection = None

	def queryAttributes(self):
		if(not self.reconnectForAttributeQuery()):
			self.btnSetExpirationTime.setEnabled(False)
			self.btnSearchComputer.setEnabled(True)
			return

		# compile query attributes
		attributes = ['SAMAccountname', 'distinguishedName']
		for title, attribute in self.GetAttributesAsDict().items():
			attributes.append(str(attribute))
		# start LDAP search
		self.connection.search(
			search_base=self.tmpDn,
			search_filter='(objectCategory=computer)',
			attributes=attributes
		)
		for entry in self.connection.entries:
			# display single result
			for title, attribute in self.GetAttributesAsDict().items():
				stringValue = str(entry[str(attribute)])

				# if this is the password attribute -> try to parse Native LAPS format
				if(str(attribute) == self.cfgLdapAttributePassword and len(entry[str(attribute)]) > 0):
					password, username, timestamp = self.parseLapsValue(entry[str(attribute)].values[0])
					if(not username or not password):
						self.printResult(str(title), password)
					else:
						self.printResult(str(title), password+'  ('+username+')  ('+timestamp+')')

				# if this is the encrypted password history attribute -> try to parse Native LAPS format
				elif(str(attribute) == self.cfgLdapAttributePasswordHistory and len(entry[str(attribute)]) > 0):
					for value in entry[str(attribute)].values:
						password, username, timestamp = self.parseLapsValue(entry[str(attribute)].values[0])
						if(not username or not password):
							self.printResult(str(title), password)
						else:
							self.printResult(str(title), password+'  ('+username+')  ('+timestamp+')')

				# if this is the expiry date attribute -> format date
				elif(str(attribute) == self.cfgLdapAttributePasswordExpiry):
					try:
						self.printResult(str(title), stringValue+' ('+str(filetime_to_dt( int(stringValue) ))+')')
					except Exception as e:
						eprint('Error:', str(e))
						self.printResult(str(title), stringValue)

				# display raw value
				else:
					self.printResult(str(title), stringValue)

			return

	def decryptPassword(self, blob):
		for server in self.cfgServer:
			try:
				import dpapi_ng
				decrypted = dpapi_ng.ncrypt_unprotect_secret(
					blob, server = server['address'],
					username = None if self.cfgUsername=='' else self.cfgUsername,
					password = None if self.cfgPassword=='' else self.cfgPassword
				)
				return decrypted.decode('utf-8').replace("\x00", "")

			except Exception as e:
				eprint('Unable to decrypt blob:', e)

	def parseLapsValue(self, ldapValue):
		try:
			# if type is bytes -> try to decrypt
			if(type(ldapValue) is bytes):
				decryptedValue = self.decryptPassword(ldapValue[16:])
				if(decryptedValue): ldapValue = decryptedValue

			# parse Native LAPS JSON
			jsonDict = json.loads(ldapValue)
			if(not 'n' in jsonDict or not 'p' in jsonDict or not 't' in jsonDict):
				raise Exception('Invalid LAPS JSON')
			return jsonDict['p'], jsonDict['n'], str(filetime_to_dt( int(jsonDict['t'], 16) ))

		except Exception as e:
			# directly use LDAP value as password (Legacy LAPS)
			return ldapValue, None, None

	def printResult(self, attribute, value):
		print((attribute+':').ljust(34)+str(value))

	def checkCredentialsAndConnect(self):
		# ask for server address and domain name if not already set via config file
		if self.cfgDomain == None:
			item = input('♕ Domain Name (e.g. example.com, leave empty to try auto discovery): ')
			if item != None:
				self.cfgDomain = item
				self.server = None
		if len(self.cfgServer) == 0:
			# query domain controllers by dns lookup
			searchDomain = '.'+self.cfgDomain if self.cfgDomain!='' else ''
			try:
				res = resolver.resolve(qname='_ldap._tcp'+searchDomain, rdtype=rdatatype.SRV, lifetime=10, search=True)
				for srv in res.rrset:
					serverEntry = {
						# strip the trailing . from the dns resolver for certificate verification reasons.
						'address': str(srv.target).rstrip('.'),
						'port': srv.port,
						'ssl': (srv.port == 636),
						'auto-discovered': True
					}
					print('DNS auto discovery found server: '+json.dumps(serverEntry))
					self.cfgServer.append(serverEntry)
			except Exception as e: print('DNS auto discovery failed: '+str(e))
			# ask user to enter server names if auto discovery was not successful
			if len(self.cfgServer) == 0:
				item = input('💻 LDAP Server Address: ')
				if item and item.strip() != '':
					self.cfgServer.append({
						'address': item,
						'port': 389,
						'ssl': False
					})
					self.server = None
		self.SaveSettings()

		# disable STARTTLS if SSL is used (otherwise, ldap3 will try to do STARTTLS on port 636)
		if len(self.cfgServer) > 0 and self.cfgServer[0]['ssl'] == True:
			self.cfgUseStartTls = False

		# establish server connection
		if self.server == None:
			try:
				serverArray = []
				for server in self.cfgServer:
					port = server['port']
					if('gc-port' in server):
						port = server['gc-port']
						self.gcModeOn = True
					serverArray.append(ldap3.Server(server['address'], port=port, use_ssl=server['ssl'], tls=self.tlsSettings, get_info=ldap3.ALL))
				self.server = ldap3.ServerPool(serverArray, ldap3.FIRST, active=True, exhaust=True)
			except Exception as e:
				print('Error connecting to LDAP server: ', str(e))
				return False

		# try to bind to server via Kerberos
		try:
			if(self.cfgUseKerberos):
				self.connection = ldap3.Connection(
					self.server,
					authentication=ldap3.SASL,
					sasl_mechanism=ldap3.GSSAPI,
					auto_referrals=True,
					auto_bind=(ldap3.AUTO_BIND_TLS_BEFORE_BIND if self.cfgUseStartTls else True)
				)
				if(self.cfgUseStartTls): self.connection.start_tls()
				return True # return if connection created successfully
		except Exception as e:
			print('Unable to connect via Kerberos: '+str(e))

		# ask for username and password for SIMPLE bind
		if self.cfgUsername == '':
			item = input('👤 Username ['+getpass.getuser()+']: ') or getpass.getuser()
			if item and item.strip() != '':
				self.cfgUsername = item
				self.connection = None
			else: return False
		if self.cfgPassword == '':
			item = getpass.getpass('🔑 Password for »'+self.cfgUsername+'«: ')
			if item and item.strip() != '':
				self.cfgPassword = item
				self.connection = None
			else: return False
		self.SaveSettings()

		# try to bind to server with username and password
		try:
			self.connection = ldap3.Connection(
				self.server,
				user=self.cfgUsername+'@'+self.cfgDomain,
				password=self.cfgPassword,
				authentication=ldap3.SIMPLE,
				auto_referrals=True,
				auto_bind=(ldap3.AUTO_BIND_TLS_BEFORE_BIND if self.cfgUseStartTls else True)
			)
			if(self.cfgUseStartTls): self.connection.start_tls()
			print('') # separate user input from results by newline
		except Exception as e:
			self.cfgUsername = ''
			self.cfgPassword = ''
			print('Error binding to LDAP server: ', str(e))
			return False

		return True

	def reconnectForAttributeQuery(self):
		# global catalog was not used for search - we can use the same connection for attribute query
		if(not self.gcModeOn): return True
		# global catalog was used for search (this buddy is read only and not all attributes are replicated into it)
		# -> that's why we need to establish a new connection to the "normal" LDAP port
		# LDAP referrals to the correct (sub)domain controller is handled automatically by ldap3
		serverArray = []
		for server in self.cfgServer:
			serverArray.append(ldap3.Server(server['address'], port=server['port'], use_ssl=server['ssl'], tls=self.tlsSettings, get_info=ldap3.ALL))
		server = ldap3.ServerPool(serverArray, ldap3.FIRST, active=True, exhaust=True)
		# try to bind to server via Kerberos
		try:
			if(self.cfgUseKerberos):
				self.connection = ldap3.Connection(server,
					authentication=ldap3.SASL,
					sasl_mechanism=ldap3.GSSAPI,
					auto_referrals=True,
					auto_bind=(ldap3.AUTO_BIND_TLS_BEFORE_BIND if self.cfgUseStartTls else True)
				)
				if(self.cfgUseStartTls): self.connection.start_tls()
				return True
		except Exception as e:
			print('Unable to connect via Kerberos: '+str(e))
		# try to bind to server with username and password
		try:
			self.connection = ldap3.Connection(server,
				user=self.cfgUsername+'@'+self.cfgDomain,
				password=self.cfgPassword,
				authentication=ldap3.SIMPLE,
				auto_referrals=True,
				auto_bind=(ldap3.AUTO_BIND_TLS_BEFORE_BIND if self.cfgUseStartTls else True)
			)
			if(self.cfgUseStartTls): self.connection.start_tls()
			return True
		except Exception as e:
			print('Error binding to LDAP server: '+str(e))
			return False

	def createLdapBase(self, conn):
		if conn.server.info:
			return conn.server.info.raw['defaultNamingContext'][0].decode('utf-8')
		elif self.cfgDomain != '':
			# convert FQDN "example.com" to LDAP path notation "DC=example,DC=com"
			search_base = ''
			base = self.cfgDomain.split('.')
			for b in base:
				search_base += 'DC=' + b + ','
			return search_base[:-1]
		else:
			raise Exception('Could not create LDAP search base: reading defaultNamingContext from LDAP directory failed and no domain given.')

	def GetConnectionString(self):
		return str(self.connection.server.host)+' '+str(self.connection.user)

	def LoadSettings(self):
		if(not path.isdir(self.cfgDir)):
			makedirs(self.cfgDir, exist_ok=True)
		# protect temporary .remmina file by limiting access to our config folder
		if(self.PLATFORM == 'linux'): os.chmod(self.cfgDir, 0o700)
		if(path.exists(self.cfgPathOld)):
			rename(self.cfgPathOld, self.cfgPath)

		if(path.isfile(self.cfgPath)): cfgPath = self.cfgPath
		elif(path.isfile(self.cfgPresetPath)): cfgPath = self.cfgPresetPath
		else: return

		try:
			with open(cfgPath) as f:
				cfgJson = json.load(f)
				self.cfgUseKerberos = cfgJson.get('use-kerberos', self.cfgUseKerberos)
				self.cfgUseStartTls = cfgJson.get('use-starttls', self.cfgUseStartTls)
				self.cfgServer = cfgJson.get('server', self.cfgServer)
				self.cfgDomain = cfgJson.get('domain', self.cfgDomain)
				self.cfgUsername = cfgJson.get('username', self.cfgUsername)
				self.cfgLdapAttributePassword = str(cfgJson.get('ldap-attribute-password', self.cfgLdapAttributePassword))
				self.cfgLdapAttributePasswordExpiry = str(cfgJson.get('ldap-attribute-password-expiry', self.cfgLdapAttributePasswordExpiry))
				self.cfgLdapAttributePasswordHistory = str(cfgJson.get('ldap-attribute-password-history', self.cfgLdapAttributePasswordHistory))
				tmpLdapAttributes = cfgJson.get('ldap-attributes', self.cfgLdapAttributes)
				if(isinstance(tmpLdapAttributes, list) or isinstance(tmpLdapAttributes, dict)):
					self.cfgLdapAttributes = tmpLdapAttributes
		except Exception as e:
			print('Error loading settings file: '+str(e))

	def SaveSettings(self):
		try:
			# do not save auto-discovered servers to config - should be queried every time
			saveServers = []
			for server in self.cfgServer:
				if not server.get('auto-discovered', False):
					saveServers.append(server)

			with open(self.cfgPath, 'w') as json_file:
				json.dump({
					'use-kerberos': self.cfgUseKerberos,
					'use-starttls': self.cfgUseStartTls,
					'server': saveServers,
					'domain': self.cfgDomain,
					'username': self.cfgUsername,
					'ldap-attribute-password': self.cfgLdapAttributePassword,
					'ldap-attribute-password-expiry': self.cfgLdapAttributePasswordExpiry,
					'ldap-attribute-password-history': self.cfgLdapAttributePasswordHistory,
					'ldap-attributes': self.cfgLdapAttributes
				}, json_file, indent=4)
		except Exception as e:
			print('Error saving settings file: '+str(e))

def eprint(*args, **kwargs):
	print(*args, file=sys.stderr, **kwargs)

def main():
	parser = argparse.ArgumentParser(epilog='© 2021-2023 Georg Sieber - https://georg-sieber.de')
	parser.add_argument('search', default=None, nargs='*', metavar='COMPUTERNAME', help='Search for this computer(s) and display the admin password. Use "*" to display all computer passwords found in LDAP directory. If you omit this parameter, the interactive shell will be started, which allows you to do multiple queries in one session.')
	parser.add_argument('-e', '--set-expiry', default=None, metavar='"2020-01-01 00:00:00"', help='Set new expiration date for computer found by search string.')
	parser.add_argument('-K', '--no-kerberos', action='store_true', help='Do not use Kerberos authentication if available, ask for LDAP simple bind credentials.')
	parser.add_argument('--version', action='store_true', help='Print version and exit.')
	args = parser.parse_args()

	cli = LapsCli(False if args.no_kerberos==True else None)

	if(args.version):
		return

	# do LDAP search by command line arguments
	if(args.search):
		validSearches = 0
		for term in args.search:
			if(term.strip() == '*'):
				cli.SearchComputer('*')
				return

			if(term.strip() != ''):
				validSearches += 1
				cli.SearchComputer(term.strip())
				if(args.set_expiry and args.set_expiry.strip() != ''):
					cli.SetExpiry(args.set_expiry.strip())

		# if at least one computername was given, we do not start the interactive shell
		if(validSearches > 0): return

	# do LDAP search by interactive shell input
	print('')
	print('Welcome to interactive shell. Please enter a computer name to search for.')
	print('Parameter --help provides more information.')
	while 1:
		# get keyboard input
		cmd = input('>> ')
		if(cmd == 'exit' or cmd == 'quit'):
			return
		else:
			cli.SearchComputer(cmd.strip())

if __name__ == '__main__':
	main()
