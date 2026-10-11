"""Single source of truth for the shared label scheme.

Topics, intents and severity follow GRADING_CONTRACT.md exactly. Subtopics are our
optional extension; issue_id = "<topic>.<subtopic>" gives stable, model-free grouping.
"""

TOPIC_DEFS = {
    "access": "Login, signup, password or account access (incl. hacked/locked accounts, verification).",
    "usability": "Navigation, controls, layout, queue/playlist management, ad interruptions.",
    "playback": "Playback failure, crashes, lag, connection failures, audio quality, battery/resource use, device/car/Bluetooth playback.",
    "downloads": "Downloading, saved music, offline listening, disappearing downloads.",
    "catalog": "Missing songs/artists, search/discovery, recommendations, lyrics availability, podcasts content.",
    "billing": "Price, charges, subscriptions, paywalls, premium entitlement; explicitly premium-only controls (skip limits, can't pick songs/forced shuffle on free) go here.",
    "support": "Contacting customer support and the support response.",
    "other": "General praise/criticism with no specific feature, unrelated content, boycott slogans, or no supported specific topic.",
}

SUBTOPICS = {
    "access": {
        "login_failure": "cannot log in / logged out / login loop / password reset fails",
        "account_compromised": "account hacked, taken over, email changed by someone else",
        "signup_verification": "cannot sign up, verify email/phone, age or region gating",
        "account_management": "deleting/merging accounts, profile or family/duo membership access",
    },
    "usability": {
        "ads": "too many/long/loud ads, ads interrupting listening",
        "ui_redesign": "layout, navigation, home screen, redesign, confusing UI",
        "queue_playlist_management": "queue, playlists, liked songs, library sorting/editing",
        "controls": "buttons, gestures, widget, lock-screen controls, sleep timer, settings",
        "other_usability": "other usability issue",
    },
    "playback": {
        "stops_or_skips": "music stops, pauses, skips by itself, won't play",
        "crash_or_freeze": "app crashes, freezes, won't open, black screen",
        "connectivity_errors": "connection/no internet errors while online, loading forever",
        "device_integration": "Bluetooth, car/Android Auto, smartwatch, speakers, Connect",
        "audio_quality": "sound quality, volume, glitches, lag in audio",
        "performance_battery": "slow, laggy UI, battery drain, storage/data use",
    },
    "downloads": {
        "downloads_disappear": "downloaded music deleted or missing",
        "download_failure": "cannot download / download stuck",
        "offline_playback": "offline mode doesn't work or asks to go online",
    },
    "catalog": {
        "missing_content": "songs/artists/albums missing or removed, greyed out",
        "search_discovery": "search doesn't find things, discovery",
        "recommendations": "bad/repetitive recommendations, algorithm, autoplay picks",
        "lyrics": "lyrics missing, paywalled lyrics, lyrics wrong",
        "podcasts_audiobooks": "podcast/audiobook content",
    },
    "billing": {
        "free_tier_restrictions": "premium-only controls: skip limits, forced shuffle, can't choose songs, features paywalled",
        "price": "too expensive, price increase",
        "charges_refunds": "unexpected/double charges, refunds, charged after cancel",
        "subscription_activation": "premium not activating, plan/entitlement lost, payment fails",
    },
    "support": {
        "support_unresponsive": "no reply, can't reach a human, bot loops",
        "support_unhelpful": "support replied but didn't solve the problem",
    },
    "other": {
        "general_praise": "general positive comment, no specific feature",
        "general_negative": "generic 'bad app' with no specific defect",
        "boycott_or_offtopic": "boycott slogans, politics, unrelated or meaningless text",
        "other_specific": "a specific point that fits no other topic",
    },
}

INTENT_DEFS = {
    "cancellation": "explicitly leaving, uninstalling, cancelling premium, switching to a competitor, or threatening to do so",
    "complaint": "negative experience, including mixed praise/criticism and generic 'bad app'",
    "request": "a desired change or feature without a reported failure",
    "praise": "positive opinion only",
    "unclear": "meaningless, unrelated, unsupported language, or bare boycott slogans without a product complaint or personal departure",
}

SEVERITY_DEFS = {
    1: "No reported problem: praise, neutral/unclear content, or a pure feature request",
    2: "Dislike, generic criticism, minor annoyance, cosmetic issue; no supported functional loss (e.g. 'too many ads', 'bad app')",
    3: "A degraded or restricted function; some use or workaround remains (e.g. skip limits, songs sometimes stop, laggy)",
    4: "A clearly blocked core task (e.g. cannot log in, app won't open, music won't play at all, downloads all gone)",
    5: "Explicit serious financial, privacy or data harm (e.g. charged without consent, account stolen, charged after cancelling); price, crashes or angry language alone are NOT 5",
}

SENTIMENT_LEVELS = (-1.0, -0.5, 0.0, 0.5, 1.0)


def issue_ids():
    return [f"{t}.{s}" for t, subs in SUBTOPICS.items() for s in subs]


def definitions_text():
    """Rubric text shared by the enricher and verifier prompts (identical by design)."""
    lines = ["TOPICS (choose exactly one; then one subtopic from that topic):"]
    for t, d in TOPIC_DEFS.items():
        lines.append(f"- {t}: {d}")
        for s, sd in SUBTOPICS[t].items():
            lines.append(f"    - {s}: {sd}")
    lines.append("\nINTENT (apply in this precedence order; pick the first that applies):")
    for i, d in INTENT_DEFS.items():
        lines.append(f"- {i}: {d}")
    lines.append("\nSEVERITY (integer 1-5; judge the reported impact, never the star rating or tone):")
    for k, d in SEVERITY_DEFS.items():
        lines.append(f"- {k}: {d}")
    return "\n".join(lines)


def consistent_severity(intent, severity):
    """Code-enforced consistency with the shared definitions: 1 = no reported problem (praise, unclear,
    pure request); complaints/cancellations report a problem, so at least 2. Applied identically to every role."""
    return 1 if intent in ("praise", "unclear", "request") else max(2, int(severity))
