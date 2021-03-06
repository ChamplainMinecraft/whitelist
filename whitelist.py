#! /usr/bin/env python3
from argparse import ArgumentParser, FileType
import json
import datetime
import os
from uuid import UUID

import googleapiclient
from googleapiclient.discovery import build
from google.oauth2 import service_account
from google.auth.transport.requests import Request

import requests

silent = False

class UserList:
    def __init__(self):
        self.users = []
        self.index = 0
    
    def add(self, user, index=None):
        """Add a User to the list

        :param user (User) The User object to add
        """
        self.users.append((index or self.index, user))
        self.index += 1
    
    def search(self, key, value):
        """Search for a user in the list given a variable to index on and the value to find
        
        :param key (string) The variable to index on
        :param value (string) The value to find
        :return (tuple) A tuple containing the index of the found item (or -1 if nothing was found), and the found object or None
        """
        for i, user in self.users:
            if user is not None and key == "email" and user.email == value or key == "username" and user.username == value or key == "uuid" and user.uuid == value:
                return (i, user)

        return (-1, None)

    @staticmethod
    def fromGoogleSheet(sheet):
        """Creates a userlist from a given list of rows from a Google Sheet

        :param sheet (list) The Google Sheet containing rows of data in the format described later in this file
        :returns (UserList) A UserList containing all users retrieved from the sheet
        """
        users = UserList()

        for i, row in enumerate(sheet.rows):
            if row is not None:
                users.add(User(email=row["email"], username=row["username"], uuid=UUID(row["uuid"])), index=i)

        return users

class User:
    def __init__(self, email=None, username=None, uuid=None):
        self.email = email
        self.username = username
        self.uuid = uuid

    def toTuple(self):
        """Returns a tuple representation of the User

        :return (tuple) The tuple representation, in the format (email, username, uuid)
        """
        return ( self.email, self.username, str(self.uuid) )

class GoogleSheet:
    def __init__(self, service, sheet_id, sheet_name, cell_range, columns):
        self.service = service
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name
        self.range_start, self.range_end = cell_range
        self.columns = columns

        self.fetch()

    def fetch(self):
        """Fetch rows from the Google Sheet given the preconfigured specifications

        :returns (GoogleSheet) After fetching, returns self
        """
        cell_range = f"{self.sheet_name}!{self.range_start}2:{self.range_end}"
        request = self.service.values().get(spreadsheetId=self.sheet_id, range=cell_range).execute()

        # Map all values to their columns within each row
        rows = []
        for cells in request.get("values", []):
            row = {}
            for i in range(min(len(cells), len(self.columns))):
                row[self.columns[i]] = cells[i]

            if row == {}:
                row = None

            rows.append(row)
        
        self.rows = rows

        return self

    def append(self, row):
        """Append a row to the sheet

        :param row (list) An array representing a row to add to the sheet
        """
        cell_range = f"{self.sheet_name}!{self.range_start}:{self.range_end}"
        body = {
            "values": row
        }
        self.service.values().append(spreadsheetId=self.sheet_id, valueInputOption="USER_ENTERED", range=cell_range, body=body).execute()

    def delete(self, row_number):
        """Delete a row from the sheet

        :param row_number (int) The number of the row to delete (0-indexed)
        """
        row_number += 1 # Converts to 1-indexed

        cell_range = f"{self.sheet_name}!{self.range_start}{row_number}:{self.range_end}{row_number}"
        self.service.values().clear(spreadsheetId=self.sheet_id, range=cell_range).execute()

class GoogleSheets:
    def __init__(self, sheet_id):
        self.sheet_id = sheet_id
        self.sheets = {}

    def login(self, credential_file):
        """Logs into the Google service account with the Google Sheets scope

        :param credential_file (string) The location of the service account credentials file
        """
        try:
            log("🔑  Attempting to log in with service account credentials from credentials.json")
            creds = service_account.Credentials.from_service_account_file(credential_file, scopes=[
                "https://www.googleapis.com/auth/spreadsheets"
            ])
        except:
            raise IOError("🔒  Failed to log in, did you specify a valid credentials.json file?")
        
        # Set up a connection to the spreadsheet
        service = build("sheets", "v4", credentials=creds)
        self.service = service.spreadsheets()

        return creds

    def store_sheet(self, internal_name, sheet_name, cell_range, columns):
        """Creates, fetches, and stores a GoogleSheet

        :param internal_name (string) The internal name to store the sheet under
        :param sheet_name (string) The identifier for the specific sheet in Google Sheets
        :param cell_range (tuple) The start and end columns for the table
        :param columns (list) A list of column headers to be mapped to each row's columns
        :return (GoogleSheet) The created GoogleSheet object
        """
        sheet = GoogleSheet(self.service, self.sheet_id, sheet_name, cell_range, columns)

        self.sheets[internal_name] = sheet
        return sheet

def log(message):
    """Log the given message if the verbosity is high enough

    :param message (string) The message to print
    """
    if not silent:
        print(message)

def sync(local, gsheets):
    # Explanation of this madness:
    # Local banlist takes precedence over remote banlist (banning is performed via /ban)
    # Remote banlist takes precedence over remote whitelist (bans propagate to the whitelist)
    # Remote whitelist takes precedence over local whitelist (whitelisting should not be performed by /whitelist add, only through form)

    # The following logic is how to merge all sources of information:
    # Get the local banlist
    # Get the remote banlist
    # Get the remote whitelist
    # For entries that are not on the remote banlist, look up any emails for the given username
    #       on the remote whitelist
    # Remove the entries from the remote whitelist, and add them to the remote banlist
    # Fetch an updated remote banlist
    # Get the remote requests
    # Check them against the remote banlist
    # If they are banned, ignore the request
    # Otherwise, add the user to the remote whitelist
    # Fetch the remote whitelist and use it to update the local whitelist
    
    # Extract the local file handles
    banlist_file, whitelist_file = local

    log(f"📂  Parsing local banlist from {banlist_file.name}")

    # Get the local banlist
    local_banlist = UserList()
    for ban in json.loads(banlist_file.read()):
        local_banlist.add(User(username=ban["name"], uuid=UUID(ban["uuid"])))

    log(f"📊  Parsing remote banlist from sheet \"{gsheets.sheets['banlist'].sheet_name}\"")

    # Get the remote banlist
    remote_banlist = UserList.fromGoogleSheet(gsheets.sheets["banlist"])

    log(f"📊  Parsing remote whitelist from sheet \"{gsheets.sheets['whitelist'].sheet_name}\"")

    # Get the remote whitelist
    remote_whitelist = UserList.fromGoogleSheet(gsheets.sheets["whitelist"])

    log("🔨  Resolving missing local ban data")

    # For entries that are not on the remote banlist, look up any emails for the given username
    #       on the remote whitelist
    for _, ban in local_banlist.users:
        row_number, user = remote_whitelist.search("username", ban.username)
        if row_number != -1:
            ban.email = user.email

    log("⏳  Processing pending bans")

    # Remove the entries from the remote whitelist, and add them to the remote banlist
    # TODO Add expiration checks and store reasons
    banlist_additions = []
    for _, ban in local_banlist.users:
        if remote_banlist.search("uuid", ban.uuid)[0] == -1:
            # Get the email for the given UUID
            row_number, reference_user = remote_whitelist.search("uuid", ban.uuid)

            # Ban all accounts added by a user
            if row_number != -1:
                while(True):
                    row_number, user = remote_whitelist.search("email", reference_user.email)

                    if row_number != -1:
                        gsheets.sheets["whitelist"].delete(row_number + 1)

                        # Append the entry to the remote banlist
                        banlist_additions.append(user.toTuple())

                        # Update the sheet so we don't keep getting the same entry over and over again when we search
                        remote_whitelist = UserList.fromGoogleSheet(gsheets.sheets["whitelist"].fetch())
                    else:
                        break

    if len(banlist_additions) > 0:
        gsheets.sheets["banlist"].append(banlist_additions)

    log(f"📊  Parsing updated remote banlist from sheet \"{gsheets.sheets['banlist'].sheet_name}\"")

    # Fetch an updated remote banlist
    remote_banlist = UserList.fromGoogleSheet(gsheets.sheets["banlist"].fetch())

    log(f"⏳  Processing new whitelist requests from sheet \"{gsheets.sheets['requests'].sheet_name}\"")

    # Get the remote requests
    whitelist_additions = []
    for request in gsheets.sheets["requests"].rows:
        if remote_banlist.search("username", request["username"])[0] == -1 and remote_whitelist.search("username", request["username"])[0] == -1:
            # Resolve the UUID using the Minecraft API
            response = requests.get(f"https://api.mojang.com/users/profiles/minecraft/{request['username']}")

            if response.status_code == 200:
                body = response.json()

                # Add the user to the remote whitelist
                user = User(email=request["email"], username=request["username"], uuid=UUID(body["id"]))
                whitelist_additions.append(user.toTuple())
            elif response.status_code >= 500:
                log("❗❗  Mojang API error")

    if len(whitelist_additions) > 0:
        gsheets.sheets["whitelist"].append(whitelist_additions)
    
    log(f"📊  Parsing updated remote whitelist from sheet \"{gsheets.sheets['whitelist'].sheet_name}\"")

    # Fetch the updated remote whitelist and use it to update the local whitelist
    remote_whitelist = UserList.fromGoogleSheet(gsheets.sheets["whitelist"].fetch())

    log("💾  Saving whitelist")

    temp_whitelist = []
    for _, user in remote_whitelist.users:
        _, username, user_id = user.toTuple()
        temp_whitelist.append({ "uuid": user_id, "name": username })

    json.dump(temp_whitelist, whitelist_file, indent=2)

    log("✅  Sync completed successfully")

def __main__():
    parser = ArgumentParser(
        description="Syncs the whitelist with an external Google sheet",
        prog="whitelist",
        epilog="In order to connect to the remote sheet, a credentials.json file needs to be in the working directory or specified by the --credentials flag")

    # Command line arguments
    parser.add_argument("sheet_id", help="The ID of the Google sheet containing the whitelisted users", type=str)
    parser.add_argument("-d", "--minecraft-folder", help="The path to the Minecraft server folder, where the whitelist and banned players files are stored", required=True, type=str)
    parser.add_argument("-c", "--credentials", help="The path to the Google Service Account credentials file", default="credentials.json", type=str)
    parser.add_argument("-w", "--whitelist", help="The path to the whitelist.json file, relative to the Minecraft server folder", default="whitelist.json", type=str)
    parser.add_argument("-b", "--banlist", help="The path to the banned-players.json file, relative to the Minecraft server folder", default="banned-players.json", type=str)
    parser.add_argument("--forms-sheet", help="The name of the form responses sheet in the spreadsheet", default="Whitelist Form Responses", type=str)
    parser.add_argument("--whitelist-sheet", help="The name of the whitelist sheet in the spreadsheet", default="Whitelist", type=str)
    parser.add_argument("--banlist-sheet", help="The name of the ban list sheet in the spreadsheet", default="Ban List", type=str)
    parser.add_argument("-s", "--silent", help="Suppress script output", action="store_true")

    args = parser.parse_args()

    # Set program verbosity
    global silent
    silent = args.silent

    # Login to the service account
    gsheets = GoogleSheets(args.sheet_id)
    gsheets.login(args.credentials)

    # Data format of each source:
    # Local whitelist:
    # | UUID | Username |
    # Local banlist:
    # | UUID | Username | Reason |
    # Form Response sheet:
    # |     A     |       B       |     C    |
    # | Timestamp | Email Address | Username |
    # Remote whitelist:
    # |       A       |     B    |   C  |
    # | Email Address | Username | UUID |
    # Remote banlist:
    # |       A       |     B    |   C  |
    # | Email address | Username | UUID |

    # Fetch the needed sheets
    gsheets.store_sheet("requests", args.forms_sheet, ("B", "C"), [ "email", "username" ])
    gsheets.store_sheet("whitelist", args.whitelist_sheet, ("A", "C"), [ "email", "username", "uuid" ])
    gsheets.store_sheet("banlist", args.banlist_sheet, ("A", "D"), [ "email", "username", "uuid" ])

    # Sync the whitelist
    banlist_file = open(os.path.join(args.minecraft_folder, args.banlist), "r")
    whitelist_file = open(os.path.join(args.minecraft_folder, args.whitelist), "w+")

    sync((banlist_file, whitelist_file), gsheets)

    banlist_file.close()
    whitelist_file.close()

__main__()
