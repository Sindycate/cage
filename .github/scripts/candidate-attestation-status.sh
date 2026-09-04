#!/usr/bin/env bash

# Return the state of the GitHub Actions attestations associated with one
# already-resolved image digest.  Verification failures are not sufficient to
# authorize recovery: an existing, but invalid, attestation must remain a
# hard failure. The repository attestation API distinguishes an absent subject
# (404) or empty list from a non-empty list while gh attestation verify enforces
# the trust policy.

candidate_attestation_state() {
  local digest="$1"
  local endpoint response status count

  if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "ERROR: cannot inspect attestations for invalid digest: ${digest}" >&2
    return 1
  fi

  endpoint="repos/${GITHUB_REPOSITORY}/attestations/${digest}?per_page=1"
  # --include keeps the HTTP status available; --jq reduces the response body
  # to the non-sensitive cardinality needed for the recovery decision.
  response="$(gh api --include --jq 'if (.attestations | type) == "array" then (.attestations | length) else error("malformed attestation response") end' "$endpoint" 2>/dev/null || true)"
  status="$(printf '%s\n' "$response" | sed -n '1s#^HTTP/[^ ]* \([0-9][0-9][0-9]\).*#\1#p')"
  if [ "$status" = "404" ]; then
    # The subject-digest endpoint returns 404 for an image with no GitHub
    # Actions attestation. Prove that the repository itself is readable before
    # treating that response as absence rather than hiding an authorization or
    # routing failure behind the same status.
    local repo_response repo_status
    repo_response="$(gh api --include --silent "repos/${GITHUB_REPOSITORY}" 2>/dev/null || true)"
    repo_status="$(printf '%s\n' "$repo_response" | sed -n '1s#^HTTP/[^ ]* \([0-9][0-9][0-9]\).*#\1#p')"
    if [ "$repo_status" != "200" ]; then
      echo "ERROR: attestation lookup returned 404 and repository access could not be confirmed (HTTP ${repo_status:-unknown}); refusing recovery" >&2
      return 1
    fi
    printf '%s' absent
    return 0
  fi
  if [ "$status" != "200" ]; then
    echo "ERROR: unable to determine attestation state for ${digest} (HTTP ${status:-unknown}); refusing recovery" >&2
    return 1
  fi

  count="$(printf '%s\n' "$response" | awk 'NF { value=$0 } END { print value }' | tr -d '\r')"
  case "$count" in
    0)
      printf '%s' absent
      ;;
    ''|*[!0-9]*)
      echo "ERROR: malformed attestation count for ${digest}; refusing recovery" >&2
      return 1
      ;;
    *)
      printf '%s' present
      ;;
  esac
}
