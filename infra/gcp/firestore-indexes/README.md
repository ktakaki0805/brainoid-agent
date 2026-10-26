# Memory vector index generations

No production model, dimension or database is selected here. Generate the exact
Firebase index manifest after OPEN-005 chooses the model and dimension:

```sh
uv run --all-packages --locked --offline python -m scripts.memory_vector_index \
  --model "$EMBEDDING_MODEL" --revision "$EMBEDDING_REVISION" \
  --dimensions "$EMBEDDING_DIMENSIONS" > /tmp/memory-vector-indexes.json
```

The command only renders JSON. It never deploys or discovers credentials. Merge its
`indexes` and `fieldOverrides` entries into the target environment's reviewed
Firestore index manifest alongside existing indexes. The generated collection name
contains the immutable configuration hash; scope partitions share its composite
index with `scope_key`, `status`, and the dimensioned flat `embedding` vector field.
`queryScope` is `COLLECTION`: runtime queries use a tenant's collection, never an
unfiltered collection-group query. Disable automatic single-field embedding indexes.

Pass the same generated manifest to `FirestoreVectorIndex`. A model, revision or
dimension change requires another generation and index. Keep the previous index
through rollback; read configuration switches only after the new generation is
ready. Cloud index deployment remains BA-OPS-02 and requires the approved target
database. Local manifest comparison does not attest the deployed index's readiness.
See [configuration, activation, fences and staging checks](../../../docs/memory-vectors.md).
