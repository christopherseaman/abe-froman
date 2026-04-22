"""Pick a randomized absurd research domain and emit it as JSON.

Stdout: {"topic": "<absurd research domain>"}
Used by the seed_topic command phase.
"""

import json
import random

TOPICS = [
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

print(json.dumps({"topic": random.choice(TOPICS)}))
