#!/usr/bin/env sh
set -eu
llm-batch azure-demo --config .azure/demo-config.json "$@"
