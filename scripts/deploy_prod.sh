#!/usr/bin/env bash
# Kalshi scanner prod deploy — P2-M4-T01 scaffold.
#
# Actions:
#   --status      Show current CloudFormation stack + ECS service state.
#   --logs        Tail CloudWatch logs for the scanner task.
#   --deploy      Build image → push to ECR → update stack (paper mode).
#   --deploy --live
#                 Same, but stack param EnvName=live. Refused unless
#                 config/kalshi_fair_value_live.json has mode=="live" AND
#                 the Secrets Manager secrets exist (kalshi/live/api-key-id
#                 and kalshi/live/private-key-pem).
#   --rollback    Roll back to the previous task definition revision.
#
# Env vars (defaults shown):
#   AWS_REGION=us-east-1
#   STACK_NAME_PAPER=kalshi-scanner-paper
#   STACK_NAME_LIVE=kalshi-scanner-live
#   ECR_REPO=kalshi-scanner

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

AWS_REGION="${AWS_REGION:-us-east-1}"
ECR_REPO="${ECR_REPO:-kalshi-scanner}"

ENV_NAME="paper"
ACTION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --status)   ACTION="status"; shift ;;
    --logs)     ACTION="logs"; shift ;;
    --deploy)   ACTION="deploy"; shift ;;
    --rollback) ACTION="rollback"; shift ;;
    --live)     ENV_NAME="live"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ -z "$ACTION" ]] && { echo "usage: $0 [--status|--logs|--deploy [--live]|--rollback]" >&2; exit 2; }

STACK_NAME=${STACK_NAME_PAPER:-kalshi-scanner-paper}
[[ "$ENV_NAME" == "live" ]] && STACK_NAME=${STACK_NAME_LIVE:-kalshi-scanner-live}

# --- Live-deploy guardrails (P2-M5 prerequisite checks) -------------------
check_live_prereqs() {
  local cfg="config/kalshi_fair_value_live.json"
  [[ -f "$cfg" ]] || { echo "missing $cfg" >&2; exit 3; }
  local mode
  mode=$(python3 -c "import json,sys; print(json.load(open('$cfg')).get('mode',''))")
  [[ "$mode" == "live" ]] || { echo "live config mode != 'live' (got '$mode')" >&2; exit 3; }
  local dry
  dry=$(python3 -c "import json,sys; print(json.load(open('$cfg')).get('dry_run',True))")
  [[ "$dry" == "False" ]] || { echo "live config dry_run must be false" >&2; exit 3; }

  # Secret manager presence (require both the key id + pem).
  aws secretsmanager describe-secret --secret-id kalshi/live/api-key-id --region "$AWS_REGION" >/dev/null 2>&1 \
    || { echo "missing Secrets Manager secret kalshi/live/api-key-id" >&2; exit 3; }
  aws secretsmanager describe-secret --secret-id kalshi/live/private-key-pem --region "$AWS_REGION" >/dev/null 2>&1 \
    || { echo "missing Secrets Manager secret kalshi/live/private-key-pem" >&2; exit 3; }

  echo "live prerequisite checks passed."
}

case "$ACTION" in
  status)
    aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" \
      --query "Stacks[0].StackStatus" --output text || true
    aws ecs describe-services --cluster default \
      --services "$STACK_NAME" --region "$AWS_REGION" \
      --query 'services[0].{Status:status,Desired:desiredCount,Running:runningCount}' \
      --output table || true
    ;;

  logs)
    aws logs tail "/kalshi/scanner/${ENV_NAME}" --region "$AWS_REGION" --follow
    ;;

  deploy)
    if [[ "$ENV_NAME" == "live" ]]; then
      echo "=== LIVE DEPLOY — running prerequisite checks ==="
      check_live_prereqs
      echo "=== proceeding (4-week P2-M4 paper-in-prod must have cleared separately) ==="
    fi

    ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
    ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"
    TAG="$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%dT%H%M%S)"

    aws ecr get-login-password --region "$AWS_REGION" \
      | docker login --username AWS --password-stdin "$ECR_URI"
    docker build -t "${ECR_URI}:${TAG}" -t "${ECR_URI}:latest" .
    docker push "${ECR_URI}:${TAG}"
    docker push "${ECR_URI}:latest"

    aws cloudformation deploy \
      --region "$AWS_REGION" \
      --stack-name "$STACK_NAME" \
      --template-file deploy/cloudformation.yml \
      --capabilities CAPABILITY_NAMED_IAM \
      --parameter-overrides "EnvName=${ENV_NAME}" "DockerImageTag=${TAG}" \
      --no-fail-on-empty-changeset
    echo "deployed image tag: ${TAG}"
    ;;

  rollback)
    SVC="$(aws ecs describe-services --cluster default --services "$STACK_NAME" \
      --region "$AWS_REGION" --query 'services[0].taskDefinition' --output text)"
    FAMILY="$(echo "$SVC" | awk -F/ '{print $2}' | awk -F: '{print $1}')"
    CUR_REV="$(echo "$SVC" | awk -F: '{print $NF}')"
    PREV_REV=$((CUR_REV - 1))
    echo "rolling $FAMILY :$CUR_REV → :$PREV_REV"
    aws ecs update-service --cluster default --service "$STACK_NAME" \
      --region "$AWS_REGION" --task-definition "${FAMILY}:${PREV_REV}" >/dev/null
    echo "rollback triggered."
    ;;
esac
