# Google Services Setup

All Google services use the same OAuth2 client credentials (client ID + client secret from a single GCP project) but obtain **separate refresh tokens** with different scopes. This means:

- One `client_secret.json` file works for all services
- Each service gets its own `.env` file (e.g. `secrets/gcal.env`, `secrets/gmail_send.env`)
- Each refresh token is scoped to a single API permission
- Revoking one token doesn't affect the others

## Setup

```bash
# Set up all services at once (encrypts automatically)
python scripts/setup-google-oauth.py --all

# Or set up specific services
python scripts/setup-google-oauth.py gmail gcal

# Encrypt with age after each OAuth flow
python scripts/setup-google-oauth.py gmail --encrypt

# Encrypt all existing .env files in secrets/ (no OAuth flow)
python scripts/setup-google-oauth.py --encrypt-all
```

## Available Services

| Service | Executor | OAuth Scope |
|---------|----------|-------------|
| Google Calendar (read) | `gcal` | `calendar.readonly` |
| Google Calendar (write) | `gcal_write` | `calendar.events` |
| Gmail (read) | `gmail_readonly` | `gmail.readonly` |
| Gmail (send) | `gmail_send` | `gmail.send` |
| Gmail (modify) | `gmail_modify` | `gmail.modify` |
| Google Drive (read) | `drive` | `drive.readonly` |
| Google Drive (write) | `drive_write` | `drive.file` |
| Google Docs | `google_docs` | `documents`, `documents.readonly` |
| Google Sheets | `google_sheets` | `spreadsheets`, `spreadsheets.readonly` |
| Google Slides | `google_slides` | `presentations`, `presentations.readonly` |

## GCP Project Setup

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the APIs you need (Calendar, Gmail, Drive, Docs, Sheets, Slides)
3. Create OAuth 2.0 credentials (Desktop application type)
4. Download the `client_secret.json` file to your project root
5. Run the setup script for each service you want to use
