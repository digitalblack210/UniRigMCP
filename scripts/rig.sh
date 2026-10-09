#!/usr/bin/env bash
# UniRig: skeleton, skin weights, then merge onto the original textured model.
# Usage: bash rig.sh /path/model.glb /path/out_dir [seed]
# Writes NAME_skeleton.fbx, NAME_skin.fbx and NAME_rigged.glb into out_dir, with a log beside them.
# The server reads the "=== stage" lines in that log to report progress, so keep their form.
set -eo pipefail
T=${UNIRIG_HOME:-$HOME/unirig}
export PATH=$T/env/bin:$PATH
export LD_LIBRARY_PATH=$T/env/lib:${LD_LIBRARY_PATH:-}
mkdir -p "$2"
in=$(readlink -f "$1"); out=$(readlink -f "$2"); seed=${3:-12345}
name=$(basename "${in%.*}")
{
  [ -d "$T/UniRig" ] || { echo "UniRig is not installed at $T. Run scripts/install.sh first."; exit 1; }
  cd "$T/UniRig"
  echo "=== skeleton $(date +%T)"
  bash launch/inference/generate_skeleton.sh --input "$in" --output "$out/${name}_skeleton.fbx" --seed "$seed"
  echo "=== skin $(date +%T)"
  bash launch/inference/generate_skin.sh --input "$out/${name}_skeleton.fbx" --output "$out/${name}_skin.fbx"
  echo "=== merge $(date +%T)"
  bash launch/inference/merge.sh --source "$out/${name}_skin.fbx" --target "$in" --output "$out/${name}_rigged.glb"
  [ -f "$out/${name}_rigged.glb" ] || { echo "UniRig finished without writing ${name}_rigged.glb."; exit 1; }
  echo "=== done $(date +%T)"
} > "$out/${name}_unirig.log" 2>&1
