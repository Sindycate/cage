#!/usr/bin/env bash

# Validate the descriptor relationship between two native architecture results
# and their assembled candidate index. With BuildKit SBOM/provenance enabled,
# each digest emitted by a native build is itself a single-platform OCI index:
# it contains one runnable manifest and one BuildKit attestation manifest with
# an unknown/unknown platform. The assembled index contains those child
# descriptors, not the architecture-index digests.

candidate_validate_descriptor_json() {
  local label="$1"
  local descriptors="$2"

  # Validate the required OCI descriptor fields and the types of every
  # optional field we preserve in the canonical identity below. Unknown
  # present fields are intentionally allowed and remain part of that identity.
  if ! printf '%s' "$descriptors" | jq -e '
    def string_map:
      type == "object" and all(to_entries[]; (.key | type == "string") and (.value | type == "string"));
    def valid_platform:
      type == "object"
      and (.architecture | type == "string")
      and (.os | type == "string")
      and ((has("os.version") | not) or (. ["os.version"] | type == "string"))
      and ((has("os.features") | not) or (. ["os.features"] | type == "array" and all(.[]; type == "string")))
      and ((has("variant") | not) or (.variant | type == "string"));
    type == "array" and length > 0 and all(.[];
      type == "object"
      and (.mediaType | type == "string" and length > 0)
      and (.digest | type == "string" and test("^sha256:[0-9a-f]{64}$"))
      and (.size | type == "number" and floor == . and . >= 0)
      and ((has("urls") | not) or (.urls | type == "array" and all(.[]; type == "string")))
      and ((has("annotations") | not) or (.annotations | string_map))
      and ((has("data") | not) or (.data | type == "string"))
      and ((has("platform") | not) or (.platform | valid_platform))
      and ((has("artifactType") | not) or (.artifactType | type == "string" and length > 0))
    )
  ' >/dev/null; then
    echo "ERROR: ${label} does not contain a valid OCI descriptor array" >&2
    return 1
  fi
}

candidate_descriptor_lines() {
  local label="$1"
  local descriptors="$2"
  local lines

  # -cS creates one compact, recursively key-sorted JSON object per line. The
  # complete object is retained: no field is projected away before comparison.
  if ! lines="$(printf '%s' "$descriptors" | jq -e -cS '
    if type == "array" and length > 0 then .[] else error("empty descriptor array") end
  ')"; then
    echo "ERROR: ${label} descriptor JSON could not be canonicalized" >&2
    return 1
  fi
  if [ -z "$lines" ]; then
    echo "ERROR: ${label} contains no child descriptors" >&2
    return 1
  fi
  printf '%s\n' "$lines"
}

candidate_reject_duplicate_descriptor_lines() {
  local label="$1"
  local lines="$2"
  local descriptor_count unique_count

  descriptor_count="$(printf '%s\n' "$lines" | awk 'NF { count++ } END { print count + 0 }')"
  unique_count="$(printf '%s\n' "$lines" | LC_ALL=C sort -u | awk 'NF { count++ } END { print count + 0 }')"
  if [ "$descriptor_count" -ne "$unique_count" ]; then
    echo "ERROR: ${label} contains duplicate child descriptors" >&2
    return 1
  fi
}

candidate_inspect_descriptor_json() {
  local ref="$1"
  local label="$2"
  local descriptors

  if ! descriptors="$(docker buildx imagetools inspect "$ref" \
      --format '{{json .Manifest.Manifests}}')"; then
    echo "ERROR: unable to inspect ${label} at ${ref}" >&2
    return 1
  fi
  printf '%s' "$descriptors"
}

candidate_inspect_descriptor_set() {
  local ref="$1"
  local label="$2"
  local descriptors lines

  if ! descriptors="$(candidate_inspect_descriptor_json "$ref" "$label")"; then
    return 1
  fi
  candidate_validate_descriptor_json "$label" "$descriptors" || return 1
  if ! lines="$(candidate_descriptor_lines "$label" "$descriptors")"; then
    return 1
  fi
  candidate_reject_duplicate_descriptor_lines "$label" "$lines" || return 1
  # Sort, but do not unique: duplicate rejection above must happen first.
  printf '%s\n' "$lines" | LC_ALL=C sort
}

candidate_inspect_architecture_descriptor_set() {
  local ref="$1"
  local label="$2"
  local expected_architecture="$3"
  local descriptors lines

  if ! descriptors="$(candidate_inspect_descriptor_json "$ref" "$label")"; then
    return 1
  fi
  candidate_validate_descriptor_json "$label" "$descriptors" || return 1

  # A native BuildKit result is exactly two descriptors: one runnable child and
  # one OCI attestation manifest linked to that runnable child. The non-
  # attestation descriptor must be the expected linux architecture, so wrong,
  # missing, duplicate, extra, or mis-linked attestation descriptors all fail.
  if ! printf '%s' "$descriptors" | jq -e --arg expected_architecture "$expected_architecture" '
    def is_attestation:
      if (.annotations? | type) != "object" then false
      else .annotations["vnd.docker.reference.type"] == "attestation-manifest"
      end;
    def is_expected_runnable:
      if (.platform? | type) != "object" then false
      else .platform.os == "linux" and .platform.architecture == $expected_architecture
      end;
    def is_exact_unknown_platform:
      if (.platform? | type) != "object" then false
      else
        .platform.os == "unknown"
        and .platform.architecture == "unknown"
        and ((.platform | keys | sort) == ["architecture", "os"])
      end;
    . as $descriptors
    | ([ $descriptors[] | select(is_attestation) ]) as $attestations
    | ([ $descriptors[] | select(is_attestation | not) ]) as $runnable_candidates
    | if ($descriptors | length) != 2 then false
      elif ($attestations | length) != 1 then false
      elif ($runnable_candidates | length) != 1 then false
      elif ([ $runnable_candidates[] | select(is_expected_runnable) ] | length) != 1 then false
      else
        ($attestations[0]) as $attestation
        | ($runnable_candidates[0]) as $runnable
        | if $attestation.mediaType != "application/vnd.oci.image.manifest.v1+json" then false
          elif ($attestation | is_exact_unknown_platform) | not then false
          elif ($attestation.annotations["vnd.docker.reference.type"] != "attestation-manifest") then false
          elif ($attestation.annotations["vnd.docker.reference.digest"] != $runnable.digest) then false
          elif $attestation.digest == $runnable.digest then false
          else true
          end
      end
  ' >/dev/null; then
    echo "ERROR: ${label} must contain exactly one ${expected_architecture} runnable child (no unexpected runnable platform) and one linked OCI BuildKit attestation" >&2
    return 1
  fi

  if ! lines="$(candidate_descriptor_lines "$label" "$descriptors")"; then
    return 1
  fi
  candidate_reject_duplicate_descriptor_lines "$label" "$lines" || return 1
  printf '%s\n' "$lines" | LC_ALL=C sort
}

# Verify that the final index contains exactly the complete child descriptors
# supplied by the two native architecture indexes. The architecture artifact
# digests remain the inputs to imagetools create; only their inspected child
# descriptors are used for this relationship check.
verify_index_matches_arch_digests() {
  local final_ref="$1"
  local label="$2"
  local expected_amd64_index="$3"
  local expected_arm64_index="$4"
  local amd64_children arm64_children expected_children actual_children

  if ! amd64_children="$(candidate_inspect_architecture_descriptor_set \
      "${final_ref%@*}@${expected_amd64_index}" \
      "${label} amd64 architecture index" amd64)"; then
    return 1
  fi
  if ! arm64_children="$(candidate_inspect_architecture_descriptor_set \
      "${final_ref%@*}@${expected_arm64_index}" \
      "${label} arm64 architecture index" arm64)"; then
    return 1
  fi

  # Each source index and the final index must be duplicate-free. Reject an
  # identical descriptor appearing in both source indexes before constructing
  # the mathematical union, so sort -u cannot hide an unexpected duplicate.
  local source_children
  source_children="$(printf '%s\n%s\n' "$amd64_children" "$arm64_children" | sed '/^$/d')"
  candidate_reject_duplicate_descriptor_lines "${label} source architecture indexes" "$source_children" || return 1
  expected_children="$(printf '%s\n' "$source_children" | LC_ALL=C sort)"
  if ! actual_children="$(candidate_inspect_descriptor_set "$final_ref" "$label")"; then
    return 1
  fi

  if [ "$actual_children" != "$expected_children" ]; then
    echo "ERROR: ${label} complete child descriptor set does not equal the union of the native architecture indexes" >&2
    echo "ERROR: expected: ${expected_children}" >&2
    echo "ERROR: actual: ${actual_children}" >&2
    return 1
  fi
}
