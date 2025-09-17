"""
Nova DN Analyzer (mechanism-aware)
- Parses variants like R273H or p.R273H
- Computes property-change features and mechanism scores
- Emits JSON plus optional compact markdown summary

CLI:
  python -m nova_dn.analyzer --seq-file path.fasta --variant p.R273H [--json]
  # or provide sequence directly
  python -m nova_dn.analyzer --seq MEEPQSDPSV --variant R273H
"""
from __future__ import annotations
import argparse
import json
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

from .amino_acid_props import delta
from .mechanisms import (
    score_interface_poisoning,
    score_active_site_jamming,
    score_structural_lattice_disruption,
    score_trafficking_maturation,
)
from .context import load_annotations_json, build_position_context
try:
    from .universal_context import UniversalContext
    UNIVERSAL_AVAILABLE = True
except ImportError:
    UNIVERSAL_AVAILABLE = False

try:
    from .dn_mechanism_filter_v2 import DNMechanismFilterV2 as DNMechanismFilter
    FILTER_AVAILABLE = True
except ImportError:
    FILTER_AVAILABLE = False


AA_SET = set("ARNDCEQGHILKMFPSTWYV")


def interpret_dn_score(score: float) -> str:
    """Interpret DN likelihood score for pathogenicity classification."""
    if score >= 0.8:
        return "Likely Pathogenic (LP)"
    elif score >= 0.5:
        return "Uncertain Significance - favor pathogenic (VUS-P)"
    elif score >= 0.3:
        return "Uncertain Significance (VUS)"
    else:
        return "Likely Benign (LB)"


def parse_variant(s: str) -> Tuple[str, int, str]:
    """Parse 'R273H' or 'p.R273H' → (ref, pos, alt). Raises ValueError on failure."""
    s = s.strip()
    if s.startswith("p."):
        s = s[2:]

    # Filter out nonsense variants (stop codons) - they can't be dominant negative
    if 'Ter' in s:
        raise ValueError("nonsense")

    if len(s) < 3:
        raise ValueError(f"Variant too short: {s}")
    ref = s[0].upper()
    alt = s[-1].upper()
    pos_str = s[1:-1]
    if ref not in AA_SET or alt not in AA_SET or not pos_str.isdigit():
        raise ValueError(f"Unrecognized variant format: {s}")
    return ref, int(pos_str), alt


@dataclass
class MechanismOutcome:
    score: float
    features: List[Dict]
    explanation: str


class NovaDNAnalyzer:
    def __init__(self, use_smart_filtering: bool = True):
        self.universal_context = UniversalContext() if UNIVERSAL_AVAILABLE else None
        self.dn_filter = DNMechanismFilter() if FILTER_AVAILABLE and use_smart_filtering else None
        self.use_smart_filtering = use_smart_filtering

    def analyze(self, sequence: str, variant: str, context: Optional[Dict] = None,
                gene_name: Optional[str] = None, uniprot_id: Optional[str] = None) -> Dict:
        sequence = sequence.strip().upper().replace("\n", "")
        # Keep letters only (drop digits/whitespace/other FASTA artifacts)
        sequence = "".join(ch for ch in sequence if ch.isalpha())
        # Sanitize non-standard/ambiguous residues often present in FASTA
        trans_map = str.maketrans({
            "O": "P",  # hydroxyproline -> proline surrogate
            "U": "C",  # selenocysteine -> cysteine surrogate
            "B": "D",  # D/N ambiguous -> aspartate
            "Z": "E",  # E/Q ambiguous -> glutamate
            "J": "L",  # I/L ambiguous -> leucine
            "X": "A",  # unknown -> alanine neutral surrogate
            "*": "",
            "?": "",
            " ": "",
        })
        sequence = sequence.translate(trans_map)
        if not sequence or any(c not in AA_SET for c in sequence):
            raise ValueError("Sequence must be a non-empty protein sequence (AAs only)")
        ref, pos1, alt = parse_variant(variant)
        if not (1 <= pos1 <= len(sequence)):
            raise ValueError(f"Variant position {pos1} out of sequence bounds (len={len(sequence)})")
        if sequence[pos1 - 1] != ref:
            # Don't hard fail; just note mismatch in context
            if context is None:
                context = {}
            context["sequence_ref_mismatch"] = True

        # Auto-generate context if not provided and universal context available
        if context is None and self.universal_context and gene_name:
            print(f"🚀 Using universal context for {gene_name} position {pos1}")
            context = self.universal_context.build_position_context(gene_name, pos1, uniprot_id)
        elif context is None:
            context = {}

        # Smart filtering: determine which mechanisms to run
        relevant_mechanisms = ["interface_poisoning", "active_site_jamming", "lattice_disruption", "trafficking_maturation"]
        filter_info = None

        if self.dn_filter and gene_name:
            print(f"🧠 Smart filtering for {gene_name}...")
            filter_result = self.dn_filter.filter_and_score(gene_name, sequence, variant, uniprot_id)
            relevant_mechanisms = filter_result["relevant_mechanisms"]
            filter_info = {
                "dn_likelihood": filter_result["dn_likelihood"],
                "recommendation": filter_result["recommendation"],
                "reasoning": filter_result["mechanism_evidence"]["reasoning"]
            }
            print(f"   DN likelihood: {filter_result['dn_likelihood']:.2f}")
            print(f"   Relevant mechanisms: {relevant_mechanisms}")

        # Compute only relevant mechanism scores
        mech_scores = {}
        all_explanations = {}

        if "interface_poisoning" in relevant_mechanisms:
            m_if = score_interface_poisoning(sequence, pos1, ref, alt, context)
            mech_scores["interface_poisoning"] = m_if[0]
            all_explanations["interface_poisoning"] = m_if[2]
        else:
            mech_scores["interface_poisoning"] = 0.0
            all_explanations["interface_poisoning"] = "mechanism filtered out"

        if "active_site_jamming" in relevant_mechanisms:
            m_as = score_active_site_jamming(sequence, pos1, ref, alt, context)
            mech_scores["active_site_jamming"] = m_as[0]
            all_explanations["active_site_jamming"] = m_as[2]
        else:
            mech_scores["active_site_jamming"] = 0.0
            all_explanations["active_site_jamming"] = "mechanism filtered out"

        if "lattice_disruption" in relevant_mechanisms:
            m_ld = score_structural_lattice_disruption(sequence, pos1, ref, alt, context)
            mech_scores["lattice_disruption"] = m_ld[0]
            all_explanations["lattice_disruption"] = m_ld[2]
        else:
            mech_scores["lattice_disruption"] = 0.0
            all_explanations["lattice_disruption"] = "mechanism filtered out"

        if "trafficking_maturation" in relevant_mechanisms:
            m_tr = score_trafficking_maturation(sequence, pos1, ref, alt, context)
            mech_scores["trafficking_maturation"] = m_tr[0]
            all_explanations["trafficking_maturation"] = m_tr[2]
        else:
            mech_scores["trafficking_maturation"] = 0.0
            all_explanations["trafficking_maturation"] = "mechanism filtered out"
        top_mech = max(mech_scores.items(), key=lambda kv: kv[1])[0]
        explanation = all_explanations[top_mech]

        # Collect contributing features from relevant mechanisms only
        contrib = []
        if "interface_poisoning" in relevant_mechanisms and "m_if" in locals():
            for f in m_if[1][:4]:
                contrib.append({"mechanism": "interface_poisoning", **f})
        if "active_site_jamming" in relevant_mechanisms and "m_as" in locals():
            for f in m_as[1][:4]:
                contrib.append({"mechanism": "active_site_jamming", **f})
        if "lattice_disruption" in relevant_mechanisms and "m_ld" in locals():
            for f in m_ld[1][:4]:
                contrib.append({"mechanism": "lattice_disruption", **f})
        if "trafficking_maturation" in relevant_mechanisms and "m_tr" in locals():
            for f in m_tr[1][:4]:
                contrib.append({"mechanism": "trafficking_maturation", **f})

        result = {
            "variant": variant,
            "position": pos1,
            "ref": ref,
            "alt": alt,
            "mechanism_scores": mech_scores,
            "top_mechanism": top_mech,
            "contributing_features": contrib,
            "explanation": explanation,
            "notes": {"sequence_ref_mismatch": bool((context or {}).get("sequence_ref_mismatch", False))},
        }

        # Add smart filtering information if available
        if filter_info:
            result["smart_filtering"] = filter_info
            result["relevant_mechanisms"] = relevant_mechanisms

        return result


# --- CLI ---

def _read_fasta(path: str) -> str:
    seq_lines: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line:
                continue
            if line.startswith(">"):
                continue
            seq_lines.append(line.strip())
    return "".join(seq_lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Nova mechanism-aware DN analyzer (Grantham-free)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--seq-file", help="Path to FASTA or plain sequence file")
    g.add_argument("--seq", help="Protein sequence string (AAs)")
    ap.add_argument("--variant", required=True, help="Variant like R273H or p.R273H")
    ap.add_argument("--annotations-json", help="Optional annotations JSON for context (resources/protein_annotations.json)")
    ap.add_argument("--protein", help="Protein key in annotations (e.g., TP53)")
    ap.add_argument("--weights-json", help="Optional weights JSON to override feature weights per mechanism")
    ap.add_argument("--gene", help="Gene name for universal context (e.g., TP53)")
    ap.add_argument("--uniprot", help="UniProt ID for universal context (e.g., P04637)")
    ap.add_argument("--json", action="store_true", help="Emit JSON only (no markdown table)")
    args = ap.parse_args(argv)

    seq = _read_fasta(args.seq_file) if args.seq_file else args.seq

    # Optional context from annotations
    context = None
    if args.annotations_json and args.protein:
        try:
            anns = load_annotations_json(args.annotations_json)
            _, pos1, _ = parse_variant(args.variant)
            context = build_position_context(anns, args.protein, pos1)
        except Exception:
            context = None

    # Optional weights override
    if args.weights_json:
        try:
            with open(args.weights_json, "r", encoding="utf-8") as wf:
                weights = json.load(wf)
            if context is None:
                context = {}
            context.setdefault("_weights", {}).update(weights)
        except Exception:
            pass

    analyzer = NovaDNAnalyzer()
    result = analyzer.analyze(seq, args.variant, context, args.gene, args.uniprot)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    # Pretty minimal markdown table + explanation
    ms = result["mechanism_scores"]
    rows = [
        ("interface_poisoning", ms["interface_poisoning"]),
        ("active_site_jamming", ms["active_site_jamming"]),
        ("lattice_disruption", ms["lattice_disruption"]),
        ("trafficking_maturation", ms["trafficking_maturation"]),
    ]
    print("| mechanism | score |\n|---|---|")
    for name, sc in rows:
        print(f"| {name} | {sc:.2f} |")
    print()

    # Add scoring interpretation
    top_score = ms[result['top_mechanism']]
    interpretation = interpret_dn_score(top_score)

    print(f"top_mechanism: {result['top_mechanism']}")
    print(f"score: {top_score:.3f}")
    print(f"interpretation: {interpretation}")
    print(f"because: {result['explanation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

