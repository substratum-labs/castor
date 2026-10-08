#!/usr/bin/env bash
set -euo pipefail

T393_MODE="${1:?expected offline-linux or intel-mac}"
case "$T393_MODE" in
  offline-linux) T393_PLATFORM=linux-amd64 ;;
  intel-mac) T393_PLATFORM=darwin-amd64 ;;
  *) printf 'unsupported T-393 mode\n' >&2; exit 2 ;;
esac

T393_ROOT="${RUNNER_TEMP}/t393-rc2"
T393_ARCHIVE="$T393_ROOT/castor-one-shot-0.1.0-rc2-${T393_PLATFORM}.tar.gz"
T393_DOCKER_CONFIG="$T393_ROOT/docker-config"
T393_DOCKER_HOST="${DOCKER_HOST:-unix:///var/run/docker.sock}"
if [[ "$T393_MODE" == intel-mac ]]; then
  test "$(uname -m)" = x86_64
  T393_DOCKER_HOST="$(docker context inspect colima --format '{{.Endpoints.docker.Host}}')"
fi
test -f "$T393_ARCHIVE"
test -f "$T393_ROOT/SHA256SUMS"
mkdir -p "$T393_ROOT/install" "$T393_DOCKER_CONFIG" "$T393_ROOT/project"
tar -xzf "$T393_ARCHIVE" -C "$T393_ROOT/install"
python3 - "$T393_ROOT/install/share/castor/release.json" <<'PY'
import json, sys
release = json.load(open(sys.argv[1]))
assert release['release_version'] == '0.1.0-rc2'
assert release['source_revision'] == 'c06e1190268df23e560acded84af968f419f550a'
PY

t393_docker() {
  env DOCKER_HOST="$T393_DOCKER_HOST" DOCKER_CONFIG="$T393_DOCKER_CONFIG" docker "$@"
}
printf 'host=%s engine=%s node=%s\n' "$(uname -m)" "$(t393_docker info --format '{{.OSType}}/{{.Architecture}} {{.ServerVersion}}')" "$(node --version)"
env DOCKER_HOST="$T393_DOCKER_HOST" DOCKER_CONFIG="$T393_DOCKER_CONFIG" \
  "$T393_ROOT/install/bin/castor" runtime prepare > "$T393_ROOT/prepare.json"
t393_docker image ls -a --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' | sort > "$T393_ROOT/images-before.txt"
t393_docker container ls -a --no-trunc --format '{{.ID}} {{.Image}} {{.Names}}' | sort > "$T393_ROOT/cids-before.txt"

T393_IPS=()
t393_restore_network() {
  if [[ "$T393_MODE" != offline-linux ]]; then return; fi
  for ip in "${T393_IPS[@]}"; do
    sudo iptables -D OUTPUT -p tcp -d "$ip" --dport 443 -j REJECT >/dev/null 2>&1 || true
  done
  if [[ -f "$T393_ROOT/hosts.before" ]]; then
    sudo cp "$T393_ROOT/hosts.before" /etc/hosts
  fi
}
trap t393_restore_network EXIT

if [[ "$T393_MODE" == offline-linux ]]; then
  sudo cp /etc/hosts "$T393_ROOT/hosts.before"
  mapfile -t T393_IPS < <(getent ahostsv4 ghcr.io | awk '{print $1}' | sort -u)
  test "${#T393_IPS[@]}" -gt 0
  for ip in "${T393_IPS[@]}"; do
    sudo iptables -I OUTPUT -p tcp -d "$ip" --dport 443 -j REJECT
  done
  printf '127.0.0.1 ghcr.io\n::1 ghcr.io\n' | sudo tee -a /etc/hosts >/dev/null
  set +e
  T393_HTTP="$(curl --noproxy '*' -sS --connect-timeout 2 --max-time 3 -o /dev/null -w '%{http_code}' https://ghcr.io/v2/ 2> "$T393_ROOT/ghcr-block.stderr")"
  T393_RC=$?
  set -e
  test "$T393_RC" -ne 0
  test "$T393_HTTP" = 000
  t393_docker info --format '{{.OSType}}/{{.Architecture}}' > "$T393_ROOT/engine-during-block.txt"
  set +e
  t393_docker pull "ghcr.io/substratum-labs/castor-controller:t393-negative-$GITHUB_RUN_ID" > "$T393_ROOT/daemon-negative.txt" 2>&1
  T393_PULL_RC=$?
  set -e
  test "$T393_PULL_RC" -ne 0
  grep -Eqi 'connect|refused|unreachable|timed out|timeout' "$T393_ROOT/daemon-negative.txt"
  printf 'registry negative controls passed; local Engine remains available\n'
fi

printf 'hello\n' > "$T393_ROOT/project/hello.txt"
cat > "$T393_ROOT/task.json" <<'JSON'
{"schema_version":1,"task_prompt":"Read hello.txt and change hello to fixed.","verification_command":["/bin/sh","-c","test \"$(cat hello.txt)\" = \"fixed\""]}
JSON
git -C "$T393_ROOT/project" init -q
git -C "$T393_ROOT/project" add hello.txt
git -C "$T393_ROOT/project" -c user.name='Castor CI' -c user.email='castor-ci@example.invalid' commit -qm fixture
set +e
env DOCKER_HOST="$T393_DOCKER_HOST" DOCKER_CONFIG="$T393_DOCKER_CONFIG" \
  CASTOR_FAKE_OLLAMA_LOG="$T393_ROOT/fake-fetch.jsonl" \
  NODE_OPTIONS="--require=$GITHUB_WORKSPACE/scripts/ci_fake_ollama.cjs" \
  "$T393_ROOT/install/bin/castor" run --project "$T393_ROOT/project" \
  --task-spec "$T393_ROOT/task.json" --model local-ollama --state-root "$T393_ROOT/state" \
  > "$T393_ROOT/result.json" 2> "$T393_ROOT/castor.stderr"
T393_RUN_RC=$?
set -e
python3 - "$T393_ROOT" "$T393_DOCKER_HOST" "$T393_DOCKER_CONFIG" "$T393_RUN_RC" <<'PY'
import json, os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
docker_env = dict(os.environ, DOCKER_HOST=sys.argv[2], DOCKER_CONFIG=sys.argv[3])
run_code = int(sys.argv[4])
result = json.loads((root / 'result.json').read_text())
task = result.get('task_result') or {}
calls = [json.loads(line) for line in (root / 'fake-fetch.jsonl').read_text().splitlines()]
cids = [result.get('controller_cid'), *(result.get('owned_cids') or [])]
summary = {
    'run_exit': run_code,
    'launcher_status': result.get('launcher_status'),
    'cleanup_status': result.get('cleanup_status'),
    'task_status': task.get('status'),
    'test_passed': task.get('test_passed'),
    'settled_actions_count': task.get('settled_actions_count'),
    'model_calls': result.get('model_calls'),
    'fake_chat_calls': sum(call.get('path') == '/api/chat' for call in calls),
    'final_patch_sha256': task.get('final_patch_sha256'),
    'owned_cids_absent': all(subprocess.run(['docker','container','inspect',cid], env=docker_env, capture_output=True).returncode != 0 for cid in cids if cid),
    'project_clean': not subprocess.check_output(['git','-C',str(root / 'project'),'status','--porcelain']),
}
(root / 'summary.json').write_text(json.dumps(summary, sort_keys=True))
print(json.dumps(summary, sort_keys=True))
assert run_code == 0 and summary['launcher_status'] == 'SUCCEEDED'
assert summary['cleanup_status'] == 'CLEAN' and summary['task_status'] == 'SUCCEEDED'
assert summary['test_passed'] is True and summary['settled_actions_count'] == 1
assert summary['model_calls'] == 2 and summary['fake_chat_calls'] == 2
assert summary['owned_cids_absent'] and summary['project_clean']
image = task.get('derived_task_image_digest')
assert image and image.startswith('sha256:')
assert not subprocess.check_output(['docker','container','ls','-a','--filter',f'ancestor={image}','--format','{{.ID}}'], env=docker_env)
labels = subprocess.check_output(['docker','image','inspect','--format','{{json .Config.Labels}}',image], env=docker_env)
observed = json.loads(labels)
assert observed['org.opencontainers.image.revision'] == 'c06e1190268df23e560acded84af968f419f550a'
run_name = pathlib.Path(result['evidence_dir']).parent.name
assert run_name.startswith('run-')
assert observed['castor.stage.owner'] == run_name.removeprefix('run-')
subprocess.run(['docker','image','rm',image], env=docker_env, check=True, stdout=subprocess.DEVNULL)
PY
t393_docker image ls -a --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' | sort > "$T393_ROOT/images-after.txt"
t393_docker container ls -a --no-trunc --format '{{.ID}} {{.Image}} {{.Names}}' | sort > "$T393_ROOT/cids-after.txt"
cmp "$T393_ROOT/images-before.txt" "$T393_ROOT/images-after.txt"
cmp "$T393_ROOT/cids-before.txt" "$T393_ROOT/cids-after.txt"
if [[ "$T393_MODE" == offline-linux ]]; then
  set +e
  T393_HTTP="$(curl --noproxy '*' -sS --connect-timeout 2 --max-time 3 -o /dev/null -w '%{http_code}' https://ghcr.io/v2/ 2>/dev/null)"
  T393_RC=$?
  set -e
  test "$T393_RC" -ne 0
  test "$T393_HTTP" = 000
  printf 'registry remained unavailable after successful task\n'
fi
printf 'T-393 %s PASS with exact resource cleanup\n' "$T393_MODE"
