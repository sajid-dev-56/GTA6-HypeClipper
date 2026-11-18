#!/usr/bin/env python3
"""
YouTube Channel Connection & OAuth Setup Assistant (connect_youtube.py)
Part of GTA6-HypeClipper Pipeline.

Guides the user through authenticating their YouTube Channel:
1. Checks for client_secrets.json or helps create it.
2. Launches Google OAuth 2.0 consent flow in the browser.
3. Obtains and saves permanent token.json.
4. Verifies the connection by displaying the connected channel name.
"""

import json
import os
import sys

CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def check_prerequisites():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        import googleapiclient.discovery
    except ImportError:
        print("\n❌ Google API client libraries missing!")
        print("Run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2\n")
        sys.exit(1)


def print_instructions():
    print("""
=============================================================================
  YOUTUBE CHANNEL CONNECTION GUIDE (2 MINUTES SETUP - 100% FREE)
=============================================================================

Google requires a free OAuth Client ID to upload videos to your YouTube channel.
Follow these 4 simple steps:

1. OPEN GOOGLE CLOUD CONSOLE:
   👉 https://console.cloud.google.com/

2. CREATE A PROJECT & ENABLE YOUTUBE API:
   - Click "Select a project" at the top -> "New Project" (name it "GTA6-Shorts").
   - In the search bar at the top, type: "YouTube Data API v3"
   - Click on it and click "ENABLE".

3. CONFIGURE OAUTH CONSENT SCREEN:
   - Go to "APIs & Services" -> "OAuth consent screen".
   - Select "External" and click "CREATE".
   - App Name: "GTA6 Shorts Clipper" | User support email: your email.
   - Click "SAVE AND CONTINUE" until "Test users".
   - Under "Test users", click "+ ADD USERS" and add YOUR Google/YouTube email!
   - Click "SAVE AND CONTINUE".

4. CREATE OAUTH CLIENT ID (CREDENTIALS):
   - Go to "APIs & Services" -> "Credentials" (left sidebar).
   - Click "+ CREATE CREDENTIALS" at the top -> "OAuth client ID".
   - Application type: Select "Desktop app".
   - Name: "GTA6 Desktop App".
   - Click "CREATE".
   - Click "DOWNLOAD JSON" on the popup.
   - Rename that downloaded file to: client_secrets.json
   - Place it inside this folder:
     """ + os.path.abspath(os.getcwd()) + """

=============================================================================
""")


def authenticate(client_secrets_path: str = CLIENT_SECRETS_FILE):
    check_prerequisites()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    import googleapiclient.discovery

    if not os.path.isfile(client_secrets_path):
        print(f"\n⚠️  '{client_secrets_path}' file nahi mila!")
        print_instructions()

        choice = input("\nKya aapke paas Client ID aur Client Secret hai? (y/N): ").strip().lower()
        if choice == "y":
            client_id = input("Enter Client ID: ").strip()
            client_secret = input("Enter Client Secret: ").strip()
            if client_id and client_secret:
                dummy_secrets = {
                    "installed": {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost", "urn:ietf:wg:oauth:2.0:oob"],
                    }
                }
                with open(client_secrets_path, "w", encoding="utf-8") as f:
                    json.dump(dummy_secrets, f, indent=2)
                print(f"✅ Created {client_secrets_path}!")
            else:
                print("❌ Invalid input. Please download client_secrets.json directly.")
                return False
        else:
            print("\nPlease download 'client_secrets.json' from Google Cloud Console and run this script again.")
            return False

    print("\n🚀 Opening browser for Google / YouTube Account Authorization...")
    print("👉 Browser me apna YouTube channel select karein aur 'Continue' / 'Allow' karein.\n")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
        creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as token_out:
            token_out.write(creds.to_json())

        print(f"✅ Authorization successful! Saved token to '{TOKEN_FILE}'.")

        # Verify connected channel
        youtube = googleapiclient.discovery.build("youtube", "v3", credentials=creds)
        channels_res = youtube.channels().list(mine=True, part="snippet").execute()

        if "items" in channels_res and len(channels_res["items"]) > 0:
            channel_title = channels_res["items"][0]["snippet"]["title"]
            print("\n" + "=" * 60)
            print(f"🎉 CONGRATULATIONS! Successfully Connected to YouTube Channel:")
            print(f"   📺 Channel: {channel_title}")
            print(f"   🚀 Ready for Automated 1080p Shorts Uploading!")
            print("=" * 60 + "\n")
            return True
        else:
            print("✅ Authenticated, but no YouTube Channel was found on this Google account.")
            return True

    except Exception as e:
        print(f"\n❌ Authentication failed: {e}")
        return False


if __name__ == "__main__":
    authenticate()
