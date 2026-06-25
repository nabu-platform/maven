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
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent


def resolve_repo_path(path_value):
	path = pathlib.Path(path_value)
	if path.is_absolute():
		return path
	return REPO_ROOT / path


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
	with open(resolve_repo_path(state_path), 'r', encoding='utf-8') as handle:
		return json.load(handle)


def ensure_states(state_path, dependencies):
	states = load_states(state_path)
	for dependency in dependencies:
		group_id = find_text(dependency, 'groupId')
		artifact_id = find_text(dependency, 'artifactId')
		if not group_id or not artifact_id:
			continue
		key = f'{group_id}:{artifact_id}'
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
		group_id = find_text(dependency, 'groupId')
		artifact_id = find_text(dependency, 'artifactId')
		if not group_id or not artifact_id:
			continue
		state_key = f'{group_id}:{artifact_id}'
		if state_key not in states:
			continue
		if states.get(state_key) == 'retired':
			print(f'Skipping retired application {state_key}')
			continue
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
				continue
			print(f'Failed to fetch package versions for {package_name}: {exc}', file=sys.stderr)
			raise
		chosen = choose_latest_version(payload)
		if chosen:
			print(f'Using latest published version for {package_name}: {chosen}')
			latest[(group_id, artifact_id)] = chosen
	return latest


def build_bom(source_path, output_path, bom_artifact_id, bom_version, owner, state_path):
	tree = ET.parse(resolve_repo_path(source_path))
	root = tree.getroot()
	dependency_management = find_child(root, 'dependencyManagement')
	if dependency_management is None:
		raise RuntimeError(f'No dependencyManagement found in {source_path}')
	dependencies_node = find_child(dependency_management, 'dependencies')
	if dependencies_node is None:
		raise RuntimeError(f'No managed dependencies found in {source_path}')
	managed_dependencies = find_children(dependencies_node, 'dependency')
	states = ensure_states(state_path, managed_dependencies)
	latest_versions = collect_latest_versions(owner, managed_dependencies, states)

	bom_root = ET.Element(qualify('project'))
	for tag, value in (
		('modelVersion', '4.0.0'),
		('groupId', find_text(root, 'groupId')),
		('artifactId', bom_artifact_id),
		('version', bom_version),
		('packaging', 'pom'),
	):
		element = ET.SubElement(bom_root, qualify(tag))
		element.text = value

	bom_dependency_management = ET.SubElement(bom_root, qualify('dependencyManagement'))
	bom_dependencies = ET.SubElement(bom_dependency_management, qualify('dependencies'))

	for dependency in managed_dependencies:
		group_id = find_text(dependency, 'groupId')
		artifact_id = find_text(dependency, 'artifactId')
		packaging = find_text(dependency, 'type') or 'zip'
		state_key = f'{group_id}:{artifact_id}'
		if state_key not in states or states.get(state_key) == 'retired':
			continue
		override = latest_versions.get((group_id, artifact_id))
		if not override:
			continue
		bom_dependency = ET.SubElement(bom_dependencies, qualify('dependency'))
		for child_tag, child_value in (
			('groupId', group_id),
			('artifactId', artifact_id),
			('version', override),
			('type', packaging),
		):
			element = ET.SubElement(bom_dependency, qualify(child_tag))
			element.text = child_value

	ET.indent(bom_root, space='  ')
	ET.ElementTree(bom_root).write(resolve_repo_path(output_path), encoding='utf-8', xml_declaration=False)


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
		print(f'Failed to generate applications BOM: {exc}', file=sys.stderr)
		raise
