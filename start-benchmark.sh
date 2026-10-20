#!/bin/bash

echo "🚀 Igniting GitHub Actions Cloud Runner..."
gh workflow run evaluate-swe-bench-full.yml

echo "⏳ Waiting for Ubuntu server to spin up..."
sleep 4 # Give GitHub 4 seconds to assign a Run ID

# Grab the ID of the workflow that just started
RUN_ID=$(gh run list --workflow evaluate-swe-bench-full.yml --limit 1 --json databaseId --jq '.[0].databaseId')

echo "📡 Connected to Cloud Runner (ID: $RUN_ID). Streaming live status..."
# Attach your terminal to the live cloud output
gh run watch $RUN_ID
