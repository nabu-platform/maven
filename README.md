# Nabu Maven Publication Flow

This repository is the central publication point for the Nabu Maven ecosystem.

It is responsible for two things:
- ingesting released module artifacts from the individual module repositories
- publishing central Maven BOMs that describe the released ecosystem state

The current flow is intentionally incremental. Only modules that have actually been released and ingested into GitHub Packages appear with concrete versions in the published BOM.

## Repositories involved

### `nabu-platform/poms`
Contains the source catalogs:
- `core.xml`
- `modules.xml`

These are still the authoritative lists of known core artifacts and modules.

### `nabu-platform/maven`
This repository contains:
- GitHub Actions workflows that publish `core-bom`, `modules-bom`, and the `modules` parent POM
- scripts that ingest module release assets into GitHub Packages
- the `modules-state.json` sidecar file
- a small explicit allowlist for hybrid module-side build plugins

This repository is also the GitHub Packages Maven host:
- `https://maven.pkg.github.com/nabu-platform/maven`

### Individual module repositories
Example:
- `nabu-platform/nabu-types-structure`

Each module repository is responsible for:
- building its own artifact from a tag
- signing it in GitHub Actions
- creating a GitHub Release with the `.nar` attached

The module repository does not publish directly into the central Maven package registry.
That is done centrally by `nabu-platform/maven`.

## High-level process

The end-to-end flow for a module is:

1. A module repository gets a release tag
2. The module workflow builds the tagged version
3. The workflow signs the produced artifact
4. The workflow creates a GitHub Release and uploads the `.nar`
5. The central `maven` workflow is run manually
6. The central workflow downloads release assets that are not yet in GitHub Packages
7. The central workflow publishes those assets into `nabu-platform/maven`
8. The central workflow regenerates the BOM from the actually published package versions
9. The central workflow publishes the new BOM snapshot

In short:
- tags create released module assets
- the central workflow ingests them into packages
- the central workflow updates the BOM to match

## Module release tagging

Module release workflows trigger on tags that match:
- `v*`

The leading `v` is stripped and the remainder becomes the Maven artifact version.

Example:
- tag: `v1.13-SNAPSHOT.20260616130811`
- published artifact version: `1.13-SNAPSHOT.20260616130811`

The version after `v` is not otherwise interpreted. It only needs to be a Maven-safe version string.

## Module repository workflow behavior

Example implementation:
- `nabu/types/structure/.github/workflows/release.yml`

That workflow does the following:
- checks out the module source
- checks out `nabu-platform/poms`
- installs `poms/modules.xml` as the parent POM into the runner-local Maven repository
- derives the release version from the tag
- runs `mvn versions:set` to align the Maven project version with the tag
- decodes the signing keystore from GitHub Actions secrets
- builds and signs the artifact
- renames the produced `.jar` payload to `.nar`
- creates a GitHub Release and uploads the `.nar`

Important details:
- module repositories build from their own tagged commit
- the release asset is the `.nar`
- the `.nar` remains self-describing because it contains Maven metadata under `META-INF/maven/...`

## Why the central repository publishes packages

GitHub Actions `GITHUB_TOKEN` cannot reliably publish packages into another repository's Maven registry.

Because of that, the chosen model is:
- module repos write releases only to their own repository
- the `maven` repo reads those releases and publishes into its own package registry

This avoids cross-repo package write problems while keeping a single central Maven endpoint.

## Central module ingestion workflow

Main workflow:
- `.github/workflows/publish-modules-bom.yml`

It performs three stages:

1. Check out this repository and `nabu-platform/poms`
2. Publish released module assets into GitHub Packages
3. Generate and publish `modules-bom`
4. Generate and publish the `modules` parent POM

### Stage 1: release asset ingestion

Script:
- `.github/scripts/publish_module_releases.py`

It reads the managed module list from:
- `poms/modules.xml`

It can also include a small explicit allowlist passed by workflow arguments for hybrid artifacts that are published with the module ecosystem but are not sourced from the full modules catalog.

For each managed dependency, it:
- maps Maven coordinates to a GitHub repository name
- checks the latest GitHub Release in that repository
- skips the module if there is no release
- skips the module if the version is already present in GitHub Packages
- downloads the `.nar` asset if needed
- extracts `groupId`, `artifactId`, and `version` from embedded `pom.properties`
- verifies that the embedded coordinates match the expected release tag version
- publishes the `.nar` into GitHub Packages using `mvn deploy:deploy-file`

Repository mapping rule:
- repository name = `groupId` with `.` replaced by `-`, then `-`, then `artifactId`

Examples:
- `nabu.types:structure` -> `nabu-types-structure`
- `nabu.frameworks:tasks` -> `nabu-frameworks-tasks`
- `nabu:maven-plugin-install` -> `nabu-maven-plugin-install`

The current explicit allowlist is intentionally small and hardcoded in the workflow:
- `nabu:maven-plugin-install:jar`
- `nabu:maven-plugin-environment:jar`

These plugin artifacts are published through the modules workflow, but they are no longer added to the generated `modules-bom`. Their versions are instead written into the generated `be.nabu:modules` parent POM.

Published package example:
- `nabu.types:structure:1.13-SNAPSHOT.20260616130811:nar`

### Stage 2: BOM generation

Script:
- `.github/scripts/generate_bom.py`

It reads the managed dependency list from:
- `poms/modules.xml` or `poms/core.xml`

It can also add a small explicit allowlist for hybrid artifacts that should appear in the published BOM without importing a much larger source catalog.

For each entry, it:
- queries GitHub Packages for published versions
- selects the newest published version by package timestamp metadata
- substitutes that concrete version into the generated BOM
- leaves the source version untouched if nothing was published yet

Published BOM example:
- `be.nabu:modules-bom:1.0-SNAPSHOT`

This BOM is a snapshot artifact. Its own top-level version stays `1.0-SNAPSHOT`, while the managed dependency entries inside it are updated to concrete released package versions.

The generated parent example is:
- `be.nabu:modules:1.0-SNAPSHOT`

That parent imports `modules-bom` and carries `pluginManagement` for the Nabu Maven plugins.

## BOM contents during bootstrap

During bootstrap, the BOM is intentionally mixed:
- released modules appear with concrete versions
- unreleased modules still retain the source catalog version expression or snapshot value from `poms/modules.xml`

Example of a released entry:
```xml
<dependency>
  <groupId>nabu.types</groupId>
  <artifactId>structure</artifactId>
  <version>1.13-SNAPSHOT.20260616130811</version>
</dependency>
```

This means the central BOM gradually becomes more concrete as more modules are released and ingested.

## Sidecar state file

File:
- `modules-state.json`

Purpose:
- track lifecycle state per managed module without modifying the source catalog structure

Current rules:
- if a module is first seen by the central scripts, an entry is added automatically as `active`
- existing entries are not modified automatically
- if a module is marked `retired`, the central scripts:
  - no longer check its releases
  - no longer include it in the generated BOM

Current file shape:
```json
{
  "nabu.types:structure": "active",
  "nabu.old:legacy": "retired"
}
```

This allows future cleanup without deleting history from `poms/modules.xml`.

## Signing

Module release workflows sign artifacts in GitHub Actions before creating the GitHub Release.

The current setup uses a GitHub-specific signing identity so CI-built artifacts are distinguishable from locally signed artifacts.

### Secret storage

Signing material is stored as GitHub organization secrets and shared only with the selected repositories.

Expected secret names:
- `SIGN_KEYSTORE_BASE64`
- `SIGN_KEYSTORE_PASSWORD`
- `SIGN_KEY_ALIAS`
- `SIGN_KEY_PASSWORD`

The working setup uses a classic JKS keystore, not PKCS12.
That was chosen because the older Maven jarsigner plugin setup behaved reliably with JKS and failed opaquely with PKCS12.

### Module workflow signing steps

The module workflow:
- decodes the Base64 keystore into a temporary file on the runner
- passes the following Maven properties:
  - `sign.keystore`
  - `sign.storetype=JKS`
  - `sign.alias`
  - `sign.storepass`
  - `sign.keypass`

The actual signing behavior comes from the shared `maven-jarsigner-plugin` configuration in `poms/modules.xml`.

## Authentication to GitHub Packages

The central `maven` workflows publish into their own repository package registry.

To make Maven authenticate reliably, the workflows create an explicit Maven `settings.xml`:
```xml
<settings>
  <servers>
    <server>
      <id>github</id>
      <username>${env.GITHUB_ACTOR}</username>
      <password>${env.GITHUB_TOKEN}</password>
    </server>
  </servers>
</settings>
```

The workflows then publish using:
- `-DrepositoryId=github`
- `-Durl=https://maven.pkg.github.com/nabu-platform/maven`

This avoids ambiguity in `setup-java` server injection.

## Current manual operations

### Release a module
1. Commit and push the module workflow if needed
2. Create a tag like:
   - `v1.13-SNAPSHOT.20260616130811`
3. Push the tag
4. Wait for the module workflow to produce a signed `.nar` GitHub Release asset

### Ingest new module releases and refresh the BOM
1. Go to `nabu-platform/maven`
2. Run workflow:
   - `Publish Modules BOM`
3. This will:
   - import missing module releases into GitHub Packages
   - regenerate `modules-bom`
   - publish the updated BOM snapshot

### Publish core BOM
1. Go to `nabu-platform/maven`
2. Run workflow:
   - `Publish Core BOM`

## What is currently implemented

Working today:
- module release tagging
- GitHub Release asset creation for modules
- GitHub CI signing with org secrets
- central release asset ingestion into GitHub Packages
- central `modules-bom` publication
- central package-state sidecar with retirement support

Known current limitation:
- the published BOM is still based on the full source catalog from `poms/modules.xml`
- only the versions of released modules are concretized automatically
- unreleased modules remain present with source-catalog version expressions or snapshot values

## Future direction

Likely next improvements:
- make the published bootstrap BOM fully incremental if desired
- add the same release flow to more modules
- bring `core` artifacts into the same central publication flow
- optionally add explicit reporting for deprecated modules alongside `retired`

## Summary

The implemented model is:
- module repositories own source builds and release assets
- the `maven` repository owns central package publication and BOM publication
- `poms` remains the source catalog of known artifacts
- `modules-state.json` lets the central system remember lifecycle state without rewriting the source catalog

This gives a phased migration path from local snapshot-based development to a centrally published Maven ecosystem without breaking existing local workflows.
