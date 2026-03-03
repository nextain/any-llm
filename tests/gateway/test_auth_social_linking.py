from fastapi.testclient import TestClient


def _social_login(
    client: TestClient,
    provider: str,
    email: str,
    provider_account_id: str,
    name: str = "Test User",
) -> None:
    response = client.post(
        "/v1/auth/login",
        json={
            "provider": provider,
            "email": email,
            "name": name,
            "provider_account_id": provider_account_id,
            "device_type": "web",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "tokens" in data
    assert "access_token" in data["tokens"]


def test_social_login_links_same_email_across_providers(
    client: TestClient,
    master_key_header: dict[str, str],
) -> None:
    email = "linking@example.com"
    _social_login(client, "google", email, "google-acc-1")
    _social_login(client, "discord", email, "discord-acc-1")

    google_lookup = client.get(
        "/v1/auth/lookup",
        params={"provider": "google", "email": email},
        headers=master_key_header,
    )
    assert google_lookup.status_code == 200
    google_user_id = google_lookup.json()["user_id"]

    discord_lookup = client.get(
        "/v1/auth/lookup",
        params={"provider": "discord", "provider_account_id": "discord-acc-1"},
        headers=master_key_header,
    )
    assert discord_lookup.status_code == 200
    discord_user_id = discord_lookup.json()["user_id"]

    assert discord_user_id == google_user_id


def test_lookup_returns_linked_accounts(
    client: TestClient,
    master_key_header: dict[str, str],
) -> None:
    """Google + Discord 같은 email → lookup 시 linked_accounts에 둘 다 포함."""
    email = "linked@example.com"
    _social_login(client, "google", email, "google-linked-1")
    _social_login(client, "discord", email, "discord-linked-1")

    resp = client.get(
        "/v1/auth/lookup",
        params={"provider": "google", "email": email},
        headers=master_key_header,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["linked_accounts"] is not None
    assert data["linked_accounts"]["discord"] == "discord-linked-1"
    assert data["linked_accounts"]["google"] == "google-linked-1"


def test_lookup_returns_no_linked_accounts_for_single_provider(
    client: TestClient,
    master_key_header: dict[str, str],
) -> None:
    """단일 프로바이더만 로그인 → linked_accounts에 해당 프로바이더만."""
    email = "single@example.com"
    _social_login(client, "google", email, "google-single-1")

    resp = client.get(
        "/v1/auth/lookup",
        params={"provider": "google", "email": email},
        headers=master_key_header,
    )
    assert resp.status_code == 200
    data = resp.json()
    # google만 linked — discord 키 없음
    linked = data.get("linked_accounts")
    if linked:
        assert "discord" not in linked


def test_social_login_keeps_separate_users_for_different_emails(
    client: TestClient,
    master_key_header: dict[str, str],
) -> None:
    _social_login(client, "google", "user-a@example.com", "google-acc-a", name="A")
    _social_login(client, "discord", "user-b@example.com", "discord-acc-b", name="B")

    user_a = client.get(
        "/v1/auth/lookup",
        params={"provider": "google", "email": "user-a@example.com"},
        headers=master_key_header,
    )
    assert user_a.status_code == 200

    user_b = client.get(
        "/v1/auth/lookup",
        params={"provider": "discord", "provider_account_id": "discord-acc-b"},
        headers=master_key_header,
    )
    assert user_b.status_code == 200

    assert user_a.json()["user_id"] != user_b.json()["user_id"]

