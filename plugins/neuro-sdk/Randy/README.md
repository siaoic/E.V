# Randy

Randy (full name Random Dot Range) is a simple websocket server designed to mimic Neuro. Note that Randy will just pick random actions and will not simulate some behaviors that Neuro could do including:
- sending actions with invalid data
- doing registered actions without actions being forced
- not *immediately* respond to forced actions

You can use Randy to test your local websocket implementations.

## Installation

1. Clone or download this repository
2. Run `npm install` in the `Randy` folder
3. Run `npm start` in the `Randy` folder

## Usage

Randy will open a websocket server on port `8000`, and a http server on port `1337`.

You can connect to it using the websocket url `ws://localhost:8000`.

You can send POST requests to the http port in order to mimic the websocket server sending that message to the client.

For example, by sending the command below, you can simulate an action being executed.

```bash
curl --request POST \
  --url http://localhost:1337/ \
  --header 'Content-Type: application/json' \
  --data '{
	"command": "action",
	"data": {
		"id": "blegh",
		"name": "join_friend_lobby",
		"data": "{\"friend_name\": \"jerma985\"}"
	}
}'
```

Randy will only send actions if he is forced to. He will not execute actions randomly. Use the above example to manually execute actions.

> [!Note]  
> Randy sometimes refuses to let go of the port when closed, not sure if it's my fault or not but I can't be bothered to fix it.  
> Just run `npx kill-port 1337` if Randy cannot start.
