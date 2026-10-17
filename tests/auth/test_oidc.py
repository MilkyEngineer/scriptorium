"""JWT validation, discovery, the JWKS cache, admin claims and the allowlist."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from scriptorium.auth.oidc import (
    DiscoveryError,
    InsufficientScope,
    OIDCValidator,
    TokenExpired,
    TokenInvalid,
    UserNotAllowed,
    claim_at_path,
    discovery_urls,
)
from tests.conftest import (
    ADMIN_SUB,
    ALLOWED_EMAIL,
    ALLOWED_SUB,
    ISSUER,
    OIDC_DISCOVERY,
    PUBLIC_URL,
    RFC8414_DISCOVERY,
    FakeClock,
    FakeIssuer,
    make_settings,
    unsigned_token,
)

# ---- valid tokens -----------------------------------------------------------------------


async def test_valid_rs256(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    user = await validator.validate(issuer.token("rsa-1"))
    assert user.iss == ISSUER
    assert user.sub == ALLOWED_SUB
    assert user.uid_hash == hashlib.sha256(f"{ISSUER}|{ALLOWED_SUB}".encode()).hexdigest()
    assert user.display_name == "reader"
    assert user.is_admin is False
    assert user.scopes == frozenset({"openid", "scriptorium", "email"})


async def test_valid_es256(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    user = await validator.validate(issuer.token("ec-1"))
    assert user.sub == ALLOWED_SUB


async def test_aud_as_string(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    user = await validator.validate(issuer.token(aud=PUBLIC_URL))
    assert user.sub == ALLOWED_SUB


async def test_aud_as_list(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    user = await validator.validate(issuer.token(aud=["account", PUBLIC_URL]))
    assert user.sub == ALLOWED_SUB


async def test_configured_audience_overrides_public_url(
    make_validator: Callable[..., OIDCValidator], issuer: FakeIssuer
) -> None:
    validator = make_validator(make_settings(oidc_audience="scriptorium-api"))
    assert await validator.validate(issuer.token(aud="scriptorium-api"))
    with pytest.raises(TokenInvalid, match="bad_audience"):
        await validator.validate(issuer.token(aud=PUBLIC_URL))


# ---- invalid claims -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "aud", ["https://mcp.example.com/mcp/", "https://other.example.com/mcp", ["account"]]
)
async def test_wrong_audience(validator: OIDCValidator, issuer: FakeIssuer, aud: Any) -> None:
    with pytest.raises(TokenInvalid, match="bad_audience"):
        await validator.validate(issuer.token(aud=aud))


@pytest.mark.parametrize(
    "iss",
    [
        "https://auth.example.com/realms/scriptorium/",
        "https://auth.example.com/realms/other",
        "https://AUTH.example.com/realms/scriptorium",
    ],
)
async def test_wrong_issuer(validator: OIDCValidator, issuer: FakeIssuer, iss: str) -> None:
    with pytest.raises(TokenInvalid, match="bad_issuer"):
        await validator.validate(issuer.token(iss=iss))


async def test_missing_audience(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    claims = issuer.claims()
    del claims["aud"]
    with pytest.raises(TokenInvalid, match="missing_aud"):
        await validator.validate(issuer.token(claims=claims))


async def test_expired(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    now = int(time.time())
    with pytest.raises(TokenExpired):
        await validator.validate(issuer.token(iat=now - 3600, exp=now - 61 - 5))


async def test_expired_within_leeway_is_accepted(
    validator: OIDCValidator, issuer: FakeIssuer
) -> None:
    now = int(time.time())
    assert await validator.validate(issuer.token(exp=now - 30))


async def test_nbf_in_future_beyond_leeway(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    with pytest.raises(TokenInvalid, match="not_yet_valid") as info:
        await validator.validate(issuer.token(nbf=int(time.time()) + 120))
    assert not isinstance(info.value, TokenExpired)


async def test_nbf_in_future_within_leeway(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    assert await validator.validate(issuer.token(nbf=int(time.time()) + 30))


async def test_missing_exp(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    claims = issuer.claims()
    del claims["exp"]
    with pytest.raises(TokenInvalid, match="missing_exp"):
        await validator.validate(issuer.token(claims=claims))


async def test_missing_sub(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    claims = issuer.claims()
    del claims["sub"]
    with pytest.raises(TokenInvalid, match="missing_sub"):
        await validator.validate(issuer.token(claims=claims))


# ---- algorithm attacks ------------------------------------------------------------------


async def test_alg_none_rejected(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    token = unsigned_token({"alg": "none", "typ": "JWT", "kid": "rsa-1"}, issuer.claims())
    with pytest.raises(TokenInvalid, match="bad_alg"):
        await validator.validate(token)
    assert issuer.jwks_hits == 0  # rejected before any network traffic


async def test_hs256_rejected(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    # The classic confusion attack: HMAC keyed with the issuer's public key.
    public_jwk = next(k for k in issuer.jwks()["keys"] if k["kid"] == "rsa-1")
    token = jwt.encode(
        issuer.claims(),
        str(public_jwk["n"]).encode(),
        algorithm="HS256",
        headers={"kid": "rsa-1"},
    )
    with pytest.raises(TokenInvalid, match="bad_alg"):
        await validator.validate(token)


async def test_alg_must_match_key_type(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    # An ES256 header pointing at the RSA key.
    rsa_signed = issuer.token("rsa-1")
    _, body, sig = rsa_signed.split(".")
    forged_header = unsigned_token({"alg": "ES256", "kid": "rsa-1"}, {}).split(".")[0]
    with pytest.raises(TokenInvalid, match="alg_key_mismatch"):
        await validator.validate(f"{forged_header}.{body}.{sig}")


async def test_bad_signature(
    validator: OIDCValidator, issuer: FakeIssuer, rsa_private_2: rsa.RSAPrivateKey
) -> None:
    token = jwt.encode(issuer.claims(), rsa_private_2, algorithm="RS256", headers={"kid": "rsa-1"})
    with pytest.raises(TokenInvalid, match="bad_signature"):
        await validator.validate(token)


@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c.d", "x" * 20000, "e30.e30.e30"])
async def test_malformed(validator: OIDCValidator, token: str) -> None:
    with pytest.raises(TokenInvalid):
        await validator.validate(token)


# ---- JWKS cache -------------------------------------------------------------------------


async def test_jwks_cached_across_tokens(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    await validator.validate(issuer.token("rsa-1"))
    await validator.validate(issuer.token("ec-1"))
    await validator.validate(issuer.token("rsa-1"))
    assert issuer.jwks_hits == 1
    assert issuer.hits[OIDC_DISCOVERY] == 1


async def test_unknown_kid_refetches_once_then_cools_down(
    validator: OIDCValidator,
    issuer: FakeIssuer,
    clock: FakeClock,
    rsa_private_2: rsa.RSAPrivateKey,
) -> None:
    await validator.validate(issuer.token("rsa-1"))
    assert issuer.jwks_hits == 1

    # A rotated key appears after the cache was filled: one refetch picks it up.
    clock.advance(61)
    issuer.add_key("rsa-2", rsa_private_2, "RS256")
    assert await validator.validate(issuer.token("rsa-2"))
    assert issuer.jwks_hits == 2

    # A second unknown kid within 60 s causes no refetch.
    issuer.add_key("rsa-3", rsa_private_2, "RS256")
    clock.advance(30)
    with pytest.raises(TokenInvalid, match="unknown_kid"):
        await validator.validate(issuer.token("rsa-3"))
    assert issuer.jwks_hits == 2

    # After the cooldown the next unknown kid refetches again.
    clock.advance(31)
    assert await validator.validate(issuer.token("rsa-3"))
    assert issuer.jwks_hits == 3


async def test_unknown_kid_storm_fetches_once(
    validator: OIDCValidator, issuer: FakeIssuer, rsa_private_2: rsa.RSAPrivateKey
) -> None:
    issuer.add_key("ghost", rsa_private_2, "RS256", publish=False)
    for _ in range(20):
        with pytest.raises(TokenInvalid, match="unknown_kid"):
            await validator.validate(issuer.token("ghost"))
    assert issuer.jwks_hits == 1


async def test_jwks_skips_unusable_keys(
    validator: OIDCValidator, issuer: FakeIssuer, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = issuer.jwks

    def jwks() -> dict[str, Any]:
        doc = real()
        doc["keys"] += [
            {"kty": "oct", "kid": "hmac", "k": "c2VjcmV0"},
            {"kty": "RSA", "kid": "enc", "use": "enc", "n": "AQAB", "e": "AQAB"},
            {"kty": "nonsense"},
            "not-a-dict",
        ]
        return doc

    monkeypatch.setattr(issuer, "jwks", jwks)
    assert await validator.validate(issuer.token("rsa-1"))
    hmac = unsigned_token({"alg": "HS256", "kid": "hmac"}, issuer.claims())
    with pytest.raises(TokenInvalid, match="bad_alg"):
        await validator.validate(hmac)


async def test_issuer_down_is_discovery_error_with_retry_cooldown(
    validator: OIDCValidator, issuer: FakeIssuer, clock: FakeClock
) -> None:
    issuer.down = True
    with pytest.raises(DiscoveryError):
        await validator.validate(issuer.token())
    hits = sum(issuer.hits.values())
    with pytest.raises(DiscoveryError, match="cooldown"):
        await validator.validate(issuer.token())
    assert sum(issuer.hits.values()) == hits  # no hammering while down
    issuer.down = False
    clock.advance(11)
    assert await validator.validate(issuer.token())


# ---- discovery --------------------------------------------------------------------------


def test_discovery_urls() -> None:
    assert discovery_urls(ISSUER) == [OIDC_DISCOVERY, RFC8414_DISCOVERY]
    assert discovery_urls("https://auth.example.com") == [
        "https://auth.example.com/.well-known/openid-configuration",
        "https://auth.example.com/.well-known/oauth-authorization-server",
    ]


async def test_rfc8414_fallback(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    issuer.serve_oidc = False
    metadata = await validator.discover()
    assert metadata.issuer == ISSUER
    assert issuer.hits[OIDC_DISCOVERY] == 1
    assert issuer.hits[RFC8414_DISCOVERY] == 1


async def test_discovery_issuer_must_match_exactly(
    validator: OIDCValidator, issuer: FakeIssuer
) -> None:
    issuer.oidc_issuer_value = ISSUER + "/"
    issuer.serve_rfc8414 = False
    with pytest.raises(DiscoveryError, match="issuer_mismatch"):
        await validator.discover()


async def test_discovery_mismatch_falls_back_to_rfc8414(
    validator: OIDCValidator, issuer: FakeIssuer
) -> None:
    issuer.oidc_issuer_value = "https://evil.example.com"
    metadata = await validator.discover()
    assert metadata.issuer == ISSUER
    assert issuer.hits[RFC8414_DISCOVERY] == 1


# ---- admin and allowlist ----------------------------------------------------------------


async def test_admin_via_realm_roles(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    user = await validator.validate(issuer.admin_token())
    assert user.sub == ADMIN_SUB
    assert user.is_admin is True


async def test_admin_role_missing(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    token = issuer.token(sub=ADMIN_SUB, realm_access={"roles": ["offline_access"]})
    assert (await validator.validate(token)).is_admin is False


async def test_admin_role_as_string_claim(
    make_validator: Callable[..., OIDCValidator], issuer: FakeIssuer
) -> None:
    validator = make_validator(make_settings(oidc_admin_claim="role", oidc_admin_value="admin"))
    assert (await validator.validate(issuer.token(role="admin"))).is_admin is True
    assert (await validator.validate(issuer.token(role="administrator"))).is_admin is False


async def test_no_admin_claim_configured_means_no_admins(
    make_validator: Callable[..., OIDCValidator], issuer: FakeIssuer
) -> None:
    validator = make_validator(make_settings(oidc_admin_claim=None, oidc_admin_value=None))
    assert (await validator.validate(issuer.admin_token())).is_admin is False


def test_claim_at_path() -> None:
    claims = {"realm_access": {"roles": ["a"]}, "https://example.com/roles": ["b"]}
    assert claim_at_path(claims, "realm_access.roles") == ["a"]
    assert claim_at_path(claims, "https://example.com/roles") == ["b"]
    assert claim_at_path(claims, "realm_access.missing") is None
    assert claim_at_path(claims, "realm_access.roles.deeper") is None


async def test_allowlisted_by_sub(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    assert (await validator.validate(issuer.token(sub=ALLOWED_SUB))).sub == ALLOWED_SUB


async def test_allowlisted_by_verified_email(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    sub = "22222222-0000-4000-8000-000000000000"
    token = issuer.token(sub=sub, email=ALLOWED_EMAIL.upper(), email_verified=True)
    user = await validator.validate(token)
    assert user.sub == sub
    assert user.email_verified is True
    assert ALLOWED_EMAIL.upper() not in repr(user)  # emails stay out of reprs


@pytest.mark.parametrize("verified", [False, "true", None])
async def test_unverified_email_rejected(
    validator: OIDCValidator, issuer: FakeIssuer, verified: object
) -> None:
    token = issuer.token(
        sub="22222222-0000-4000-8000-000000000000", email=ALLOWED_EMAIL, email_verified=verified
    )
    with pytest.raises(UserNotAllowed):
        await validator.validate(token)


async def test_unknown_user_rejected(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    with pytest.raises(UserNotAllowed):
        await validator.validate(issuer.token(sub="33333333-0000-4000-8000-000000000000"))


async def test_allow_all_issuer_users(
    make_validator: Callable[..., OIDCValidator], issuer: FakeIssuer
) -> None:
    validator = make_validator(make_settings(oidc_allowed_users="", allow_all_issuer_users=True))
    sub = "33333333-0000-4000-8000-000000000000"
    assert (await validator.validate(issuer.token(sub=sub))).sub == sub


async def test_custom_user_claim(
    make_validator: Callable[..., OIDCValidator], issuer: FakeIssuer
) -> None:
    validator = make_validator(
        make_settings(oidc_user_claim="oid", oidc_allowed_users="object-id-1")
    )
    user = await validator.validate(issuer.token(oid="object-id-1"))
    assert user.sub == "object-id-1"
    assert user.uid_hash == hashlib.sha256(f"{ISSUER}|object-id-1".encode()).hexdigest()


async def test_display_name_falls_back(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    token = issuer.token(preferred_username=None)
    assert (await validator.validate(token)).display_name == ALLOWED_SUB[:8]


async def test_scp_list_claim(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    token = issuer.token(scope=None, scp=["scriptorium", 7])
    assert (await validator.validate(token)).scopes == frozenset({"scriptorium"})


async def test_scp_string_claim(validator: OIDCValidator, issuer: FakeIssuer) -> None:
    token = issuer.token(scope=None, scp="scriptorium openid")
    assert (await validator.validate(token)).scopes == frozenset({"scriptorium", "openid"})


async def test_no_scope_claim_is_fine_by_default(
    validator: OIDCValidator, issuer: FakeIssuer
) -> None:
    assert (await validator.validate(issuer.token(scope=None))).scopes == frozenset()


@pytest.mark.parametrize(
    "claims",
    [
        {"scope": "openid scriptorium"},
        {"scope": None, "scp": "scriptorium"},
        {"scope": None, "scp": ["scriptorium"]},
        {"scope": "openid", "scp": ["scriptorium"]},
    ],
)
async def test_required_scope_present(
    make_validator: Callable[..., OIDCValidator], issuer: FakeIssuer, claims: dict[str, Any]
) -> None:
    validator = make_validator(make_settings(oidc_required_scopes="scriptorium"))
    assert await validator.validate(issuer.token(**claims))


@pytest.mark.parametrize(
    "claims",
    [{"scope": "openid email"}, {"scope": None}, {"scope": None, "scp": ["openid"]}],
)
async def test_required_scope_missing(
    make_validator: Callable[..., OIDCValidator], issuer: FakeIssuer, claims: dict[str, Any]
) -> None:
    validator = make_validator(make_settings(oidc_required_scopes="scriptorium"))
    with pytest.raises(InsufficientScope):
        await validator.validate(issuer.token(**claims))


# ---- privacy ----------------------------------------------------------------------------


async def test_logs_hold_no_token_or_email(
    validator: OIDCValidator, issuer: FakeIssuer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="scriptorium")
    good = issuer.token(email=ALLOWED_EMAIL, email_verified=True)
    await validator.validate(good)
    bad = issuer.token(sub="33333333-0000-4000-8000-000000000000", email="x@example.com")
    with pytest.raises(UserNotAllowed):
        await validator.validate(bad)
    text = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    for secret in (good, bad, ALLOWED_EMAIL, "x@example.com", ALLOWED_SUB):
        assert secret not in text
