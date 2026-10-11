"""The qualification slice — disqualifiers as rows, the rollup judgment, the priority score (T7).

``rules`` evaluates a manifest's free predicates; ``judgment`` is the ``classify_rollup`` Agent SDK
node; ``scoring`` turns cited signal answers into a priority; ``service`` remembers every
disqualification so a rejected business is never re-sourced; ``dry_run`` is the manifest quality
check that ``activate`` requires.
"""
