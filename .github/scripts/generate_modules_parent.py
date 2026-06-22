#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

MAVEN_NS = 'http://maven.apache.org/POM/4.0.0'
ET.register_namespace('', MAVEN_NS)

PLUGIN_COORDINATES = [
	('nabu', 'maven-plugin-install'),
	('nabu', 'maven-plugin-environment'),
]


def qualify(tag):
	return f'{{{MAVEN_NS}}}{tag}'


def latest_version(owner, group_id, artifact_id):
	token = os.environ.get('GITHUB_TOKEN')
	if not token:
		raise RuntimeError('GITHUB_TOKEN is required')
	package_name = f'{group_id}.{artifact_id}'
	url = (
		f'https://api.github.com/orgs/{owner}/packages/maven/'
		f'{urllib.parse.quote(package_name, safe="")}/versions?per_page=100'
	)
	request = urllib.request.Request(url)
	request.add_header('Accept', 'application/vnd.github+json')
	request.add_header('Authorization', f'Bearer {token}')
	request.add_header('X-GitHub-Api-Version', '2022-11-28')
	try:
		with urllib.request.urlopen(request) as response:
			payload = json.load(response)
	except urllib.error.HTTPError as exc:
		if exc.code == 404:
			return None
		print(f'Failed to fetch package versions for {package_name}: {exc}', file=sys.stderr)
		raise
	latest = None
	for entry in payload:
		name = entry.get('name')
		if not name:
			continue
		published_at = entry.get('updated_at') or entry.get('created_at') or ''
		candidate = (published_at, name)
		if latest is None or candidate > latest:
			latest = candidate
	return latest[1] if latest is not None else None


def add_text(parent, tag, value):
	element = ET.SubElement(parent, qualify(tag))
	element.text = value
	return element


def build_parent(output_path, version, owner):
	plugin_versions = {}
	for group_id, artifact_id in PLUGIN_COORDINATES:
		resolved = latest_version(owner, group_id, artifact_id)
		if resolved is None:
			raise RuntimeError(f'No published plugin version found for {group_id}:{artifact_id}')
		plugin_versions[(group_id, artifact_id)] = resolved

	root = ET.Element(qualify('project'))
	for tag, value in (
		('modelVersion', '4.0.0'),
		('groupId', 'be.nabu'),
		('artifactId', 'modules'),
		('version', version),
		('packaging', 'pom'),
	):
		add_text(root, tag, value)

	dependency_management = ET.SubElement(root, qualify('dependencyManagement'))
	dependencies = ET.SubElement(dependency_management, qualify('dependencies'))
	dependency = ET.SubElement(dependencies, qualify('dependency'))
	add_text(dependency, 'groupId', 'be.nabu')
	add_text(dependency, 'artifactId', 'modules-bom')
	add_text(dependency, 'version', version)
	add_text(dependency, 'type', 'pom')
	add_text(dependency, 'scope', 'import')

	build = ET.SubElement(root, qualify('build'))
	plugin_management = ET.SubElement(build, qualify('pluginManagement'))
	plugins = ET.SubElement(plugin_management, qualify('plugins'))
	for group_id, artifact_id in PLUGIN_COORDINATES:
		plugin = ET.SubElement(plugins, qualify('plugin'))
		add_text(plugin, 'groupId', group_id)
		add_text(plugin, 'artifactId', artifact_id)
		add_text(plugin, 'version', plugin_versions[(group_id, artifact_id)])

	active_plugins = ET.SubElement(build, qualify('plugins'))
	install_plugin = ET.SubElement(active_plugins, qualify('plugin'))
	add_text(install_plugin, 'groupId', 'nabu')
	add_text(install_plugin, 'artifactId', 'maven-plugin-install')
	executions = ET.SubElement(install_plugin, qualify('executions'))
	execution = ET.SubElement(executions, qualify('execution'))
	goals = ET.SubElement(execution, qualify('goals'))
	add_text(goals, 'goal', 'install-dependencies')

	ET.indent(root, space='  ')
	ET.ElementTree(root).write(output_path, encoding='utf-8', xml_declaration=False)


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('--output', required=True)
	parser.add_argument('--version', required=True)
	parser.add_argument('--owner', required=True)
	args = parser.parse_args()
	try:
		build_parent(args.output, args.version, args.owner)
	except Exception as exc:
		print(f'Failed to generate modules parent: {exc}', file=sys.stderr)
		raise
