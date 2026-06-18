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
NS = {'m': MAVEN_NS}
ET.register_namespace('', MAVEN_NS)


def qualify(tag):
	return f'{{{MAVEN_NS}}}{tag}'


def find_child(element, tag):
	if element is None:
		return None
	child = element.find(f'm:{tag}', NS)
	if child is not None:
		return child
	return element.find(tag)


def find_children(element, tag):
	if element is None:
		return []
	children = element.findall(f'm:{tag}', NS)
	if children:
		return children
	return element.findall(tag)


def find_text(element, tag):
	child = find_child(element, tag)
	return child.text if child is not None else None


def load_states(state_path):
	with open(state_path, 'r', encoding='utf-8') as handle:
		return json.load(handle)


def save_states(state_path, states):
	with open(state_path, 'w', encoding='utf-8') as handle:
		json.dump(states, handle, indent=2, sort_keys=True)
		handle.write('\n')


def ensure_states(state_path, dependencies):
	states = load_states(state_path)
	for dependency in dependencies:
		groupId = find_text(dependency, 'groupId')
		artifactId = find_text(dependency, 'artifactId')
		if not groupId or not artifactId:
			continue
		key = f'{groupId}:{artifactId}'
		if key not in states:
			continue
	return states


def choose_latest_version(payload):
	latest_entry = None
	for entry in payload:
		name = entry.get('name')
		if not name:
			continue
		published_at = entry.get('updated_at') or entry.get('created_at') or ''
		candidate = (published_at, name)
		if latest_entry is None or candidate > latest_entry:
			latest_entry = candidate
	return latest_entry[1] if latest_entry is not None else None


def collect_latest_versions(owner, dependencies, states):
	token = os.environ.get('GITHUB_TOKEN')
	if not token:
		raise RuntimeError('GITHUB_TOKEN is required')
	latest = {}
	for dependency in dependencies:
		groupId = find_text(dependency, 'groupId')
		artifactId = find_text(dependency, 'artifactId')
		if not groupId or not artifactId:
			continue
		state_key = f'{groupId}:{artifactId}'
		if state_key not in states:
			continue
		if states.get(state_key) == 'retired':
			print(f'Skipping retired module {state_key}')
			continue
		packageName = f'{groupId}.{artifactId}'
		url = (
			f'https://api.github.com/orgs/{owner}/packages/maven/'
			f'{urllib.parse.quote(packageName, safe="")}/versions?per_page=100'
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
				continue
			print(f'Failed to fetch package versions for {packageName}: {exc}', file=sys.stderr)
			raise
		chosen = choose_latest_version(payload)
		if chosen:
			print(f'Using latest published version for {packageName}: {chosen}')
			latest[(groupId, artifactId)] = chosen
	return latest


def build_bom(sourcePath, outputPath, bomArtifactId, bomVersion, owner, statePath):
	tree = ET.parse(sourcePath)
	root = tree.getroot()
	dependencyManagement = find_child(root, 'dependencyManagement')
	if dependencyManagement is None:
		raise RuntimeError(f'No dependencyManagement found in {sourcePath}')
	dependenciesNode = find_child(dependencyManagement, 'dependencies')
	if dependenciesNode is None:
		raise RuntimeError(f'No managed dependencies found in {sourcePath}')
	managedDependencies = find_children(dependenciesNode, 'dependency')
	states = ensure_states(statePath, managedDependencies)
	latestVersions = collect_latest_versions(owner, managedDependencies, states)

	bomRoot = ET.Element(qualify('project'))
	for tag, value in (
		('modelVersion', '4.0.0'),
		('groupId', find_text(root, 'groupId')),
		('artifactId', bomArtifactId),
		('version', bomVersion),
		('packaging', 'pom'),
	):
		element = ET.SubElement(bomRoot, qualify(tag))
		element.text = value

	bomDependencyManagement = ET.SubElement(bomRoot, qualify('dependencyManagement'))
	bomDependencies = ET.SubElement(bomDependencyManagement, qualify('dependencies'))

	defaultType = 'nar' if bomArtifactId == 'modules-bom' else None
	for dependency in managedDependencies:
		groupId = find_text(dependency, 'groupId')
		artifactId = find_text(dependency, 'artifactId')
		version = find_text(dependency, 'version')
		typeValue = find_text(dependency, 'type') or defaultType
		scopeValue = find_text(dependency, 'scope')
		state_key = f'{groupId}:{artifactId}'
		if state_key not in states:
			continue
		if states.get(state_key) == 'retired':
			continue
		override = latestVersions.get((groupId, artifactId))
		bomDependency = ET.SubElement(bomDependencies, qualify('dependency'))
		for childTag, childValue in (
			('groupId', groupId),
			('artifactId', artifactId),
			('version', override if override else version),
		):
			element = ET.SubElement(bomDependency, qualify(childTag))
			element.text = childValue
		if typeValue:
			element = ET.SubElement(bomDependency, qualify('type'))
			element.text = typeValue
		if scopeValue:
			element = ET.SubElement(bomDependency, qualify('scope'))
			element.text = scopeValue

	ET.indent(bomRoot, space='  ')
	ET.ElementTree(bomRoot).write(outputPath, encoding='utf-8', xml_declaration=False)


if __name__ == '__main__':
	parser = argparse.ArgumentParser()
	parser.add_argument('--source', required=True)
	parser.add_argument('--output', required=True)
	parser.add_argument('--owner', required=True)
	parser.add_argument('--state', required=True)
	parser.add_argument('--bom-artifact-id', required=True)
	parser.add_argument('--bom-version', required=True)
	args = parser.parse_args()
	try:
		build_bom(args.source, args.output, args.bom_artifact_id, args.bom_version, args.owner, args.state)
	except Exception as exc:
		print(f'Failed to generate BOM: {exc}', file=sys.stderr)
		raise
