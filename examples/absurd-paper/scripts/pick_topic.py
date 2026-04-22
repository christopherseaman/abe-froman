"""Emit a bundle of absurd research inspirations as JSON.

Stdout: {"inspirations": ["...", "...", ...]}

Draws a handful of seed prompts from two buckets:
  - curated absurd domains (original topic list)
  - JIR-style faux article stubs (in the spirit of the Journal of
    Irreproducible Results, which has been publishing deadpan absurd
    science since 1955)

The downstream `choose_topic` prompt phase reads these and synthesizes
a single absurd research domain. Splitting seed → synthesis exercises
the command→prompt handoff and gives Claude room to riff instead of
picking verbatim from a list.
"""

import json
import random

ABSURD_DOMAINS = [
    "the quantum mechanics of spilled coffee",
    "the fluid dynamics of sock puppets at conversational speeds",
    "the thermodynamics of elevator small talk",
    "the linguistic complexity of canine sighs",
    "the game theory of parallel parking",
    "the epidemiology of autocorrect typos",
    "the statistical mechanics of tangled headphone cables",
    "the ergonomics of asking 'who has seen my phone' while holding it",
    "the sociolinguistics of the phrase 'per my last email'",
    "the evolutionary biology of sandwich preferences",
    "the non-Euclidean geometry of IKEA instruction manuals",
    "the rheology of pancake batter at open-mic nights",
    "the microeconomics of shared office refrigerators",
    "the phonetics of frustrated toddler negotiations",
    "the topology of shoelace failure modes",
    "the migratory patterns of lost single socks",
    "the semiotics of grocery-store background music",
    "the bureaucratic anthropology of the DMV waiting room",
    "the nutritional paradox of conference buffet strategy",
    "the psychoacoustics of apartment upstairs-neighbor footsteps",
    "the meteorology of indoor plant mortality",
    "the decision theory of choosing a movie theater seat",
    "the behavioral ecology of suburban squirrels at bird feeders",
    "the anthropology of potluck casserole labels",
    "the kinematics of supermarket shopping cart drift",
    "the forensic accounting of group-dinner bill splits",
    "the chronobiology of Monday morning coffee preparation",
    "the hydraulics of ketchup bottle diplomacy",
    "the paleography of grandparent birthday card handwriting",
]

# JIR-flavored faux article stubs — the deadpan TOC-entry style that the
# Journal of Irreproducible Results has perfected since 1955. These are
# NOT real JIR articles; they're pastiche seeds to nudge Claude toward
# that register.
JIR_STUBS = [
    "On the Persistence of Crumbs in Keyboards: A Longitudinal Study",
    "Toward a Unified Theory of Why the Dryer Eats Socks",
    "Statistical Anomalies in the Distribution of 'Good' Pens at Meetings",
    "A Preliminary Taxonomy of Sighs Emitted During Tax Preparation",
    "Do Refrigerator Magnets Exhibit Emergent Herd Behavior?",
    "The Observed Correlation Between Umbrella Ownership and Rainfall Cessation",
    "Quantifying the Social Half-Life of an Unaddressed Email Chain",
    "Vending Machine Hysteresis: A Case Report",
    "The Unreasonable Effectiveness of Percussive Maintenance",
    "Queueing Theory of the 'Sorry, Go Ahead' Doorway Standoff",
    "On the Apparent Non-Conservation of Tupperware Lids",
    "Birdbath Fluid Dynamics Under Sparrow-Induced Perturbation",
    "Does the Office Printer Know When You Are in a Hurry? A Pilot Study",
    "The Improbable Longevity of Free Conference Pens",
    "A Gravitational Anomaly Observed Near Half-Eaten Cake in Break Rooms",
    "Self-Organized Criticality in Pantry Spice Jar Arrangements",
    "Empirical Evidence for the 'Last Cookie Hesitation' Phenomenon",
    "Thermal Imaging of Competitive Thermostat Adjustment Behavior",
    "Lexical Drift in Group Text Threads Over Holiday Weekends",
    "Why Do Grocery Bags Always Split on the Walk From the Car?",
    "A Bayesian Analysis of 'I'll Just Have a Bite of Yours'",
    "Intergenerational Transmission of Leftover Container Anxiety",
]


def main() -> None:
    # 3 curated absurd domains + 3 JIR-style stubs = 6 seeds.
    # Enough variety for the synthesis prompt to riff, not so many that
    # the prompt just picks one verbatim.
    inspirations = random.sample(ABSURD_DOMAINS, 3) + random.sample(JIR_STUBS, 3)
    random.shuffle(inspirations)
    print(json.dumps({"inspirations": inspirations}))


if __name__ == "__main__":
    main()
