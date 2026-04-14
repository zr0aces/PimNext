#!/bin/bash
set -e

# Configure CUPS client to point to the correct server
if [ -n "$CUPS_SERVER" ]; then
    echo "Configuring CUPS client to use server: $CUPS_SERVER"
    echo "ServerName $CUPS_SERVER" > $CUPS_CONF
else
    echo "CUPS_SERVER not set, using default 'cups'"
    echo "ServerName cups" > $CUPS_CONF
fi

# Execute the application
exec "$@"
