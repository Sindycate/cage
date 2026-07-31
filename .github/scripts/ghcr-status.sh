# Authoritative GHCR manifest existence check (sourced by release workflows).
#
# `docker buildx imagetools inspect` exit codes do not distinguish "manifest
# absent" from authentication failures, rate limits, timeouts, or registry
# outages, and parsing its free-form error text is unsafe: an image reference
# (e.g. a commit SHA that happens to contain "404") or a credential-helper or
# network message can contain words such as "404" or "not found". Treating any
# of those as absence would let a CI rerun overwrite an immutable candidate or
# version tag.
#
# Instead, query the registry HTTP API directly and branch on the structured
# status code:
#   200 -> manifest present
#   404 -> manifest authoritatively absent (the ONLY result that authorizes
#          creating a write-once tag)
#   anything else (401/403/5xx/timeout/000) -> ambiguous; callers fail closed.
#
# Requires GH_TOKEN (a GITHUB_TOKEN) and GITHUB_ACTOR in the environment.
# Usage: ghcr_status <repository-path> <reference>
#   e.g. ghcr_status sindycate/cage/base "candidate-${SHA}"
# Echoes a numeric HTTP status code (000 if the registry could not be reached).
ghcr_status() {
  gs_repo="$1"
  gs_ref="$2"
  gs_token="$(curl -q -fsS -u "${GITHUB_ACTOR}:${GH_TOKEN}" \
    "https://ghcr.io/token?service=ghcr.io&scope=repository:${gs_repo}:pull" \
    | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' || true)"
  if [ -z "$gs_token" ]; then
    printf '000'
    return 0
  fi
  gs_code="$(curl -q -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${gs_token}" \
    -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json" \
    "https://ghcr.io/v2/${gs_repo}/manifests/${gs_ref}" || true)"
  printf '%s' "${gs_code:-000}"
}
