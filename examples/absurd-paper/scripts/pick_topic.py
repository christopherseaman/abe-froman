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
    "the computational complexity of grocery list optimization",
    "the epidemiology of autocorrect typos",
    "the statistical mechanics of tangled headphone cables",
    "the cognitive load of choosing a streaming service password",
    "the ergonomics of asking 'who has seen my phone' while holding it",
    "the sociolinguistics of the phrase 'per my last email'",
    "the evolutionary biology of sandwich preferences",
    "the cryptographic entropy of middle-seat armrest negotiations",
    "the non-Euclidean geometry of IKEA instruction manuals",
]

print(json.dumps({"topic": random.choice(TOPICS)}))
