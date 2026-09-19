"""Known API providers: how to recognise them in code, and what to probe.

`detect` is matched against what the scanner finds in a call site (URL text,
env var names, imported packages). `probes` are side-effect-free GET endpoints
polled for drift - never a mutating endpoint.
"""

from __future__ import annotations

import os
from typing import Any

from .config import settings


def _registry() -> list[dict[str, Any]]:
    acme = settings.acme_orders_url.rstrip("/")
    return [
        {
            "id": "acme-orders",
            "category": "orders", "tokens": ["ORDERS"], "key_prefixes": [], "api_docs_url": None,
            "name": "Acme Orders",
            "detect": {"hosts": ["localhost:4010", "api.acme-orders.dev"], "env": ["ORDERS_API_URL"], "packages": []},
            "base_url": acme,
            "probes": [{"id": "orders.list", "method": "GET", "path": "/v1/orders"}],
            "docs_url": None,  # discovered from the Link rel="deprecation" header or the release webhook
            "validation_env": {"ORDERS_API_URL": acme},
        },
        {
            "id": "elevenlabs",
            "category": "voice", "tokens": ["ELEVENLABS", "ELEVEN_LABS", "XI_API"], "key_prefixes": [], "api_docs_url": "https://elevenlabs.io/docs/api-reference/introduction",
            "name": "ElevenLabs",
            "detect": {"hosts": ["api.elevenlabs.io"], "env": ["ELEVENLABS_API_KEY"], "packages": ["elevenlabs", "@elevenlabs/elevenlabs-js"]},
            "base_url": "https://api.elevenlabs.io",
            "probes": [{"id": "voices.list", "method": "GET", "path": "/v1/voices", "auth": {"header": "xi-api-key", "env": "ELEVENLABS_API_KEY"}}],
            "docs_url": "https://elevenlabs.io/docs/changelog",
        },
        {
            "id": "nebius",
            "category": "llm", "tokens": ["NEBIUS"], "key_prefixes": [], "api_docs_url": "https://docs.nebius.com/studio/inference/api",
            "name": "Nebius AI Studio",
            "detect": {"hosts": ["api.studio.nebius.com", "api.studio.nebius.ai", "api.tokenfactory.nebius.com"], "env": ["NEBIUS_API_KEY"], "packages": []},
            "base_url": "https://api.studio.nebius.com",
            "probes": [{"id": "models.list", "method": "GET", "path": "/v1/models", "auth": {"header": "Authorization", "env": "NEBIUS_API_KEY", "prefix": "Bearer "}}],
            "docs_url": "https://docs.nebius.com/studio/inference/api",
        },
        {"id": "stripe", "category": "payments", "tokens": ["STRIPE"], "key_prefixes": ["sk_live_", "sk_test_", "rk_live_", "rk_test_", "pk_live_", "pk_test_"], "api_docs_url": "https://docs.stripe.com/api",  "name": "Stripe", "detect": {"hosts": ["api.stripe.com"], "env": ["STRIPE_SECRET_KEY", "STRIPE_API_KEY"], "packages": ["stripe"]}, "base_url": "https://api.stripe.com", "probes": [], "docs_url": "https://docs.stripe.com/upgrades"},
        {"id": "twilio", "category": "messaging", "tokens": ["TWILIO"], "key_prefixes": [], "api_docs_url": "https://www.twilio.com/docs/usage/api",  "name": "Twilio", "detect": {"hosts": ["api.twilio.com", "verify.twilio.com"], "env": ["TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID"], "packages": ["twilio"]}, "base_url": "https://api.twilio.com", "probes": [], "docs_url": "https://www.twilio.com/en-us/changelog"},
        {"id": "openai", "category": "llm", "tokens": ["OPENAI", "OPEN_AI", "GPT"], "key_prefixes": ["sk-proj-", "sk-svcacct-"], "api_docs_url": "https://platform.openai.com/docs/api-reference/chat/create",  "name": "OpenAI", "detect": {"hosts": ["api.openai.com"], "env": ["OPENAI_API_KEY"], "packages": ["openai"]}, "base_url": "https://api.openai.com", "probes": [], "docs_url": "https://platform.openai.com/docs/changelog"},
        {"id": "anthropic", "category": "llm", "tokens": ["ANTHROPIC", "CLAUDE"], "key_prefixes": ["sk-ant-"], "api_docs_url": "https://docs.anthropic.com/en/api/messages",  "name": "Anthropic", "detect": {"hosts": ["api.anthropic.com"], "env": ["ANTHROPIC_API_KEY"], "packages": ["anthropic", "@anthropic-ai/sdk"]}, "base_url": "https://api.anthropic.com", "probes": [], "docs_url": "https://docs.anthropic.com/en/release-notes/api"},
        {"id": "gemini", "category": "llm", "tokens": ["GEMINI", "GOOGLE_AI", "GOOGLE_GENAI"], "key_prefixes": ["AIza"], "api_docs_url": "https://ai.google.dev/api/generate-content",  "name": "Gemini", "detect": {"hosts": ["generativelanguage.googleapis.com"], "env": ["GEMINI_API_KEY"], "packages": ["@google/genai", "@google/generative-ai", "google-genai"]}, "base_url": "https://generativelanguage.googleapis.com", "probes": [], "docs_url": "https://ai.google.dev/gemini-api/docs/changelog"},
        {"id": "perplexity", "name": "Perplexity", "category": "llm", "tokens": ["PERPLEXITY", "PPLX"], "key_prefixes": ["pplx-"], "api_docs_url": "https://docs.perplexity.ai/api-reference/chat-completions-post", "detect": {"hosts": ["api.perplexity.ai"], "env": ["PERPLEXITY_API_KEY"], "packages": []}, "base_url": "https://api.perplexity.ai", "probes": [], "docs_url": "https://docs.perplexity.ai/changelog/changelog"},
        {"id": "clerk", "name": "Clerk", "category": "auth", "tokens": ["CLERK"], "key_prefixes": [], "api_docs_url": "https://clerk.com/docs/reference/backend-api", "detect": {"hosts": ["api.clerk.com", "api.clerk.dev"], "env": ["CLERK_SECRET_KEY", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"], "packages": ["@clerk/nextjs", "@clerk/backend", "@clerk/clerk-sdk-node", "@clerk/clerk-react"]}, "base_url": "https://api.clerk.com", "probes": [], "docs_url": "https://clerk.com/changelog"},
        {"id": "supabase", "name": "Supabase", "category": "database", "tokens": ["SUPABASE"], "key_prefixes": ["sbp_", "sb_secret_", "sb_publishable_"], "api_docs_url": "https://supabase.com/docs/reference/javascript/introduction", "detect": {"hosts": ["supabase.co", "supabase.in"], "env": ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"], "packages": ["@supabase/supabase-js", "@supabase/ssr"]}, "base_url": None, "probes": [], "docs_url": "https://supabase.com/changelog"},
        {"id": "github", "category": "vcs", "tokens": ["GITHUB", "GH_TOKEN"], "key_prefixes": ["ghp_", "github_pat_", "gho_", "ghs_"], "api_docs_url": "https://docs.github.com/en/rest",  "name": "GitHub", "detect": {"hosts": ["api.github.com"], "env": [], "packages": ["octokit", "@octokit/rest"]}, "base_url": "https://api.github.com", "probes": [], "docs_url": "https://docs.github.com/en/rest/about-the-rest-api/breaking-changes"},
        {"id": "shopify", "category": "commerce", "tokens": ["SHOPIFY"], "key_prefixes": ["shpat_", "shpss_"], "api_docs_url": "https://shopify.dev/docs/api",  "name": "Shopify", "detect": {"hosts": ["myshopify.com"], "env": ["SHOPIFY_API_KEY"], "packages": ["@shopify/shopify-api"]}, "base_url": None, "probes": [], "docs_url": "https://shopify.dev/docs/api/release-notes"},
    ]


def all_providers() -> list[dict[str, Any]]:
    return _registry()


def get_provider(provider_id: str) -> dict[str, Any] | None:
    return next((p for p in _registry() if p["id"] == provider_id), None)


def probe_headers(probe: dict[str, Any]) -> dict[str, str] | None:
    """Auth headers for a probe, or None when the key it needs is not configured."""
    auth = probe.get("auth")
    if not auth:
        return {}
    key = os.environ.get(auth["env"])
    if not key:
        return None
    return {auth["header"]: f"{auth.get('prefix', '')}{key}"}


def match_provider(url_text: str, env_names: set[str], packages: set[str]) -> dict[str, Any] | None:
    for provider in _registry():
        detect = provider["detect"]
        if any(host in url_text for host in detect["hosts"]):
            return provider
        if env_names & set(detect["env"]):
            return provider
        if packages & set(detect["packages"]):
            return provider
    return None
