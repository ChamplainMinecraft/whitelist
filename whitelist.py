#! /usr/bin/env python3
from argparse import ArgumentParser, FileType
import json
import datetime
import os.path

import googleapiclient
from googleapiclient.discovery import build
from google.oauth2 import service_account
from google.auth.transport.requests import Request

verbose = False

class UserList:
    def __init__(self):
        self.users = []
    
    def add(self, user):
        """Add a User to the list

        :param user (User) The User object to add
        """
        self.users.append(user)
    
    def search(self, key, value):
        """Search for a user in the list given a variable to index on and the value to find
        
        :param key (string) The variable to index on
        :param value (string) The value to find
        """
        for user in self.users:
            if user[key] == value:
                return user

class User:
    def __init__(self, email=None, username=None, uuid=None):
        self.email = email
        self.username = username
        self.uuid = uuid

class Local:
    def __init__(self, banlist_file, whitelist_file):
        self.banlist = banlist_file
        self.whitelist = whitelist_file

class GoogleSheet:
    def __init__(self, service, sheet_id, cell_range, columns):
        self.service = service
        self.sheet_id = sheet_id
        self.range = cell_range
        self.columns = columns

        self.fetch()

    def fetch(self):
        """Fetch rows from the Google Sheet given the preconfigured specifications
        """
        request = self.service.values().get(spreadsheetId=self.sheet_id, range=range).execute()

        # Map all values to their columns within each row
        rows = []
        for row in request.get("values", []):
            row = {}
            for i in range(min(len(rows), len(self.columns))):
                row[self.columns[i]] = rows[i]

            rows.append(row)
        
        self.rows = rows

class GoogleSheets:
    def __init__(self, sheet_id):
        self.sheet_id = sheet_id
        self.sheets = {}

    def login(self, credential_file):
        """Logs into the Google service account with the Google Sheets scope

        :param credential_file (string) The location of the service account credentials file
        """
        try:
            log("Attempting to log in with service account credentials from credentials.json")
            creds = service_account.Credentials.from_service_account_file(credential_file, scopes=[
                "https://www.googleapis.com/auth/spreadsheets"
            ])
        except:
            print("Failed to log in, did you specify a valid credentials.json file?")
        
        # Set up a connection to the spreadsheet
        service = build("sheets", "v4", credentials=creds)
        self.service = service.spreadsheets()

        return creds
    
    def store_sheet(self, name, cell_range, columns):
        self.sheets[name] = GoogleSheet(self.service, self.sheet_id, cell_range, columns)

def log(message):
    """Log the given message if the verbosity is high enough

    :param message (string) The message to print
    """
    if verbose:
        print(message)

def sync(local, sheets):
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
    
    # Get the local banlist
    local_banlist = UserList()
    for ban in json.loads(local.banlist.read()):
        local_banlist.add(User(username=ban["name"], uuid=ban["uuid"]))

    # Get the remote banlist
    remote_banlist = UserList()
    for ban in sheets.sheets["banlist"].rows:
        remote_banlist.add(User(email=ban["email"], username=ban["username"], uuid=ban["uuid"]))

    # Get the remote whitelist
    remote_whitelist = UserList()
    for user in sheets.sheets["whitelist"].rows:
        remote_whitelist.add(User(email=user["email"], username=user["username"], uuid=user["uuid"]))

    # For entries that are not on the remote banlist, look up any emails for the given username
    #       on the remote whitelist
    for ban in local_banlist.users:
        user = remote_whitelist.search("username", ban.username)
        if user:
            ban.email = user.email
    
    # Remove the entries from the remote whitelist, and add them to the remote banlist
    # TODO Remote from remote whitelist
    # TODO Add to the remote banlist

    # Fetch an updated remote banlist
    sheets.sheets["banlist"].fetch()

    # Get the remote requests
    for response in sheets.sheets["requests"].rows:
        if remote_banlist.search("email", response[1]) is None:
            # TODO Resolve the UUID
            # TODO Add to remote whitelist
            pass
    
    # Fetch the remote whitelist and use it to update the local whitelist
    sheets.sheets["whitelist"].fetch()
    # TODO Save to local whitelist

def __main__():
    parser = ArgumentParser(
        description="Syncs the whitelist with an external Google sheet",
        prog="whitelist",
        epilog="In order to connect to the remote sheet, a credentials.json file needs to be in the working directory or specified by the --credentials flag")

    # Command line arguments
    parser.add_argument("sheet_id", help="The ID of the Google sheet containing the whitelisted users", type=str)
    parser.add_argument("-c", "--credentials", help="The path to the Google Service Account credentials file", default="credentials.json", type=str)
    parser.add_argument("-w", "--whitelist", help="The path to the whitelist.json file", default="whitelist.json", type=FileType("w"))
    parser.add_argument("-b", "--banlist", help="The path to the banned-players.json file", default="banned-players.json", type=FileType("w"))
    parser.add_argument("--forms-sheet", help="The name of the form responses sheet in the spreadsheet", default="Whitelist Form Responses", type=str)
    parser.add_argument("--whitelist-sheet", help="The name of the whitelist sheet in the spreadsheet", default="Whitelist", type=str)
    parser.add_argument("--banlist-sheet", help="The name of the ban list sheet in the spreadsheet", default="Ban List", type=str)
    parser.add_argument("-v", "--verbose", help="Show more verbose output", action="store_true")

    args = parser.parse_args()

    # Set program verbosity
    global verbose
    verbose = args.verbose
    log("Running in verbose mode")
    log(f"Arguments: {args}")

    # Open local files
    local_files = Local(args.banlist, args.whitelist)

    # Login to the service account
    sheets = GoogleSheets(args.sheet_id)
    sheets.login(args.credentials)

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
    sheets.store_sheet("requests", f"{args.forms_sheet}!B1:C", [ "email", "username" ])
    sheets.store_sheet("whitelist", f"{args.whitelist_sheet}!A1:C", [ "email", "username", "uuid" ])
    sheets.store_sheet("banlist", f"{args.banlist_sheet}A1:D", [ "email", "username", "uuid" ])

    # Sync the whitelist
    sync(local_files, sheets)

__main__()
