"""
Modern Agent Core
=================

A clean, modern AGENT runtime for Scholar Navis.

Modules:
    - skill_registry: self-describing Skill metadata registry (wraps SkillManager).
    - planner:        intent analysis + optional semantic focus hint (never filters).
    - runtime:        the plan-execute-observe Agent loop (also records provenance
                      and streams plot results to the UI).
    - decomposer:     breaks a complex research query into ordered sub-tasks (Deep Mode).
    - synthesizer:    merges sub-task findings into one coherent, cited answer (Deep Mode).

Modern AGENT principles followed here:
    1. Tool selection is delegated to the main LLM's NATIVE function calling —
       the planner never strips tools with brittle keyword matching.
    2. All user-enabled Skills are exposed, so the LLM keeps full agency.
    3. A single lightweight semantic-focus call may suggest "most relevant"
       tools as a prompt hint (guidance only) when the set is large.
    4. Execution runs through a clean, bounded plan->execute->observe loop.
    5. Deep Mode composes decomposition + parallel execution + synthesis for
       multi-part research questions, with citation remapping across stages.
"""
