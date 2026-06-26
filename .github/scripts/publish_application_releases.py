#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

MAVEN_NS = 'http://maven.apache.org/POM/4.0.0'
NS = {'m': MAVEN_NS}
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent


def resolve_repo_path(path_value):
	path = pathlib.Path(path_value)
	if path.is_absolute():
		return path
	return REPO_ROOT / path


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


def github_request(url):
	token = os.environ.get('GITHUB_TOKEN')
	if not token:
		raise RuntimeError('GITHUB_TOKEN is required')
	request = urllib.request.Request(url)
	request.add_header('Accept', 'application/vnd.github+json')
	request.add_header('Authorization', f'Bearer {token}')
	request.add_header('X-GitHub-Api-Version', '2022-11-28')
	with urllib.request.urlopen(request) as response:
		return json.load(response)


def load_states(state_path):
	path = resolve_repo_path(state_path)
	if not path.exists():
		return {}
	with open(path, 'r', encoding='utf-8') as handle:
		return json.load(handle)


def save_states(state_path, states):
	with open(resolve_repo_path(state_path), 'w', encoding='utf-8') as handle:
		json.dump(states, handle, indent=2, sort_keys=True)
		handle.write('\n')


def ensure_states(state_path, dependencies):
	states = load_states(state_path)
	changed = False
	for group_id, artifact_id, _, _ in dependencies:
		key = f'{group_id}:{artifact_id}'
		if key not in states:
			states[key] = 'released'
			changed = True
	if changed:
		save_states(state_path, states)
	return states


PLUGIN_ARTIFACTS = [
	('nabu', 'maven-plugin-application', 'jar'),
]


def load_managed_dependencies(source_path):
	root = ET.parse(resolve_repo_path(source_path)).getroot()
	dependency_management = find_child(root, 'dependencyManagement')
	if dependency_management is None:
		raise RuntimeError(f'No dependencyManagement found in {source_path}')
	dependencies_node = find_child(dependency_management, 'dependencies')
	if dependencies_node is None:
		raise RuntimeError(f'No managed dependencies found in {source_path}')
	result = []
	for dependency in find_children(dependencies_node, 'dependency'):
		group_id = find_text(dependency, 'groupId')
		artifact_id = find_text(dependency, 'artifactId')
		version = find_text(dependency, 'version')
		type_value = find_text(dependency, 'type') or 'zip'
		if group_id and artifact_id and version:
			result.append((group_id, artifact_id, version, type_value))
	for group_id, artifact_id, type_value in PLUGIN_ARTIFACTS:
		result.append((group_id, artifact_id, '1.0-SNAPSHOT', type_value))
	return result


def to_repo_name(group_id, artifact_id):
	return artifact_id


def find_latest_release(owner, repo):
	url = f'https://api.github.com/repos/{owner}/{repo}/releases/latest'
	try:
		return github_request(url)
	except urllib.error.HTTPError as exc:
		if exc.code == 404:
			return None
		if exc.code >= 500:
			print(f'  failed to query latest release for {repo}: {exc}', file=sys.stderr)
			return None
		raise


def package_has_version(owner, package_name, version):
	url = f'https://api.github.com/orgs/{owner}/packages/maven/{urllib.parse.quote(package_name, safe="")}/versions?per_page=100'
	try:
		versions = github_request(url)
	except urllib.error.HTTPError as exc:
		if exc.code == 404:
			return False
		raise
	for entry in versions:
		if entry.get('name') == version:
			return True
	return False


def download_asset(asset, output_dir):
	url = asset['url']
	filename = asset['name']
	request = urllib.request.Request(url)
	request.add_header('Accept', 'application/octet-stream')
	request.add_header('Authorization', f'Bearer {os.environ["GITHUB_TOKEN"]}')
	request.add_header('X-GitHub-Api-Version', '2022-11-28')
	path = output_dir / filename
	with urllib.request.urlopen(request) as response, open(path, 'wb') as target:
		target.write(response.read())
	return path


def deploy_file(asset_path, group_id, artifact_id, version, packaging):
	cmd = [
		'mvn', '-B', 'deploy:deploy-file',
		'-Durl=https://maven.pkg.github.com/nabu-platform/maven',
		'-DrepositoryId=github',
		f'-Dfile={asset_path}',
		f'-DgroupId={group_id}',
		f'-DartifactId={artifact_id}',
		f'-Dversion={version}',
		f'-Dpackaging={packaging}',
	]
	subprocess.run(cmd, check=True)


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument('--source', required=True)
	parser.add_argument('--owner', required=True)
	parser.add_argument('--state', required=True)
	args = parser.parse_args()
	output_dir = REPO_ROOT / 'downloaded-application-releases'
	output_dir.mkdir(exist_ok=True)
	dependencies = load_managed_dependencies(args.source)
	states = ensure_states(args.state, dependencies)
	for group_id, artifact_id, _, packaging in dependencies:
		state_key = f'{group_id}:{artifact_id}'
		if states.get(state_key) == 'retired':
			print(f'Skipping retired application {state_key}')
			continue
		package_name = f'{group_id}.{artifact_id}'
		repo_name = to_repo_name(group_id, artifact_id)
		print(f'Checking {package_name} from repo {repo_name}')
		latest_release = find_latest_release(args.owner, repo_name)
		if latest_release is None:
			print(f'  no release found for {repo_name}')
			continue
		version = latest_release.get('tag_name', '').removeprefix('v')
		if not version:
			print(f'  latest release for {repo_name} has no usable tag')
			continue
		if package_has_version(args.owner, package_name, version):
			print(f'  package version already present: {package_name}:{version}')
			continue
		asset_suffix = '.' + packaging
		asset = next((asset for asset in latest_release.get('assets', []) if asset.get('name', '').endswith(asset_suffix)), None)
		if asset is None:
			print(f'  no {asset_suffix} release asset found for {repo_name}:{version}')
			continue
		print(f'  downloading asset {asset.get("name")}')
		asset_path = download_asset(asset, output_dir)
		print(f'  publishing {package_name}:{version} to GitHub Maven as {packaging}')
		try:
			deploy_file(asset_path, group_id, artifact_id, version, packaging)
		except subprocess.CalledProcessError:
			if package_has_version(args.owner, package_name, version):
				print(f'  package version became available during publish: {package_name}:{version}')
			else:
				raise


if __name__ == '__main__':
	try:
		main()
	except Exception as exc:
		print(f'Failed to publish application releases: {exc}', file=sys.stderr)
		raise
