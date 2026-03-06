"""
Cadence — Cognito Custom Auth Triggers
Handles passwordless email OTP login via CUSTOM_AUTH flow.

Three trigger handlers routed by triggerSource:
  - DefineAuthChallenge: orchestrates challenge sequence
  - CreateAuthChallenge: generates OTP and sends via SES
  - VerifyAuthChallengeResponse: validates user's code
"""

from __future__ import annotations

import os
import random


def handler(event, context):
    """Route to the appropriate trigger handler."""
    source = event.get("triggerSource", "")
    if source == "DefineAuthChallenge_Authentication":
        return define_auth_challenge(event)
    elif source == "CreateAuthChallenge_Authentication":
        return create_auth_challenge(event)
    elif source == "VerifyAuthChallengeResponse_Authentication":
        return verify_auth_challenge(event)
    return event


def define_auth_challenge(event):
    """Decide whether to issue a challenge, grant tokens, or fail."""
    sessions = event["request"].get("session", [])

    if not sessions:
        # No challenges yet — issue a custom challenge
        event["response"]["challengeName"] = "CUSTOM_CHALLENGE"
        event["response"]["issueTokens"] = False
        event["response"]["failAuthentication"] = False
    elif len(sessions) <= 3 and sessions[-1].get("challengeResult") is True:
        # Answered correctly — grant tokens
        event["response"]["issueTokens"] = True
        event["response"]["failAuthentication"] = False
    elif len(sessions) >= 3:
        # Too many attempts — fail
        event["response"]["issueTokens"] = False
        event["response"]["failAuthentication"] = True
    else:
        # Wrong answer — issue another challenge (retry)
        event["response"]["challengeName"] = "CUSTOM_CHALLENGE"
        event["response"]["issueTokens"] = False
        event["response"]["failAuthentication"] = False

    return event


def create_auth_challenge(event):
    """Generate a 6-digit OTP and email it to the user."""
    import boto3

    code = f"{random.randint(100000, 999999)}"
    email = event["request"]["userAttributes"].get("email", "")
    sender = os.environ.get("SES_SENDER", "")
    region = os.environ.get("AWS_REGION", "eu-west-2")

    if email and sender:
        ses = boto3.client("ses", region_name=region)
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": "Your Cadence login code"},
                "Body": {
                    "Text": {
                        "Data": (
                            f"Your login code is: {code}\n\n"
                            "This code expires in 3 minutes.\n"
                            "If you didn't request this, ignore this email."
                        )
                    }
                },
            },
        )

    event["response"]["publicChallengeParameters"] = {"email": email}
    event["response"]["privateChallengeParameters"] = {"answer": code}
    event["response"]["challengeMetadata"] = f"CODE-{code}"
    return event


def verify_auth_challenge(event):
    """Check whether the user's answer matches the expected code."""
    expected = event["request"]["privateChallengeParameters"].get("answer", "")
    actual = event["request"].get("challengeAnswer", "")
    event["response"]["answerCorrect"] = bool(expected and expected == actual)
    return event
