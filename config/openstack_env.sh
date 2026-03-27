#!/usr/bin/env bash
# OpenStack credentials for Infomaniak S3 access.
# ─────────────────────────────────────────────────────────────────────────────
# DO NOT COMMIT THIS FILE WITH REAL CREDENTIALS.
# This template is committed; copy it, fill in your values, and source it:
#
#   cp config/openstack_env.sh config/openstack_env.local.sh
#   # edit openstack_env.local.sh with real values
#   source config/openstack_env.local.sh
#
# Then run: gooroo-registry publish --dry-run
# ─────────────────────────────────────────────────────────────────────────────

export OS_AUTH_URL="https://api.pub1.infomaniak.cloud/identity"
export OS_PROJECT_NAME="your-project-name"
export OS_USERNAME="your-username"
export OS_PASSWORD="your-password"
export OS_USER_DOMAIN_NAME="Default"
export OS_PROJECT_DOMAIN_NAME="Default"
export OS_REGION_NAME="dc3-a"
