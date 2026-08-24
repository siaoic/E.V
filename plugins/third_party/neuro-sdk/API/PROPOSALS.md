# Neuro API Proposals

This file serves as an overview on what messages might be added in the future.

> [!Important]  
> These message types have been proposed to be added to the API, but have not been implemented yet.  
> For all intents and purposes, they do not exist yet! So don't try to use them.

## Incoming Messages (S2C, Neuro to Game)

### Reregister All Actions

If there is a problem mid-game and Neuro crashes, upon reconnection this message might be sent in order to reregister all actions that were previously registered. You should respond to this with an actions register containing all actions that are currently supposed to be registered.

```ts
{
    "command": "actions/reregister_all"
}
```

### Graceful Shutdown

> [!Note]
> This is part of the game automation API, which will only be used for games that Neuro can launch by herself.  
> As such, most games will not need to implement this.

This message will be sent when Neuro decides to stop playing a game, or upon manual intervention from the dashboard. You should create or identify graceful shutdown points where the game can be closed gracefully after saving progress. You should store the latest received `wants_shutdown` value, and if it is `true` when a graceful shutdown point is reached, you should save the game and quit to main menu, then send back a shutdown ready message.

> [!Important]  
> Please don't actually close the game, just quit to main menu.  
> Neuro will close the game herself.

```ts
{
    "command": "shutdown/graceful",
    "data": {
        "wants_shutdown": boolean
    }
}
```

#### Parameters
- `wants_shutdown`: Whether the game should shutdown at the next graceful shutdown point. `true` means shutdown is requested, `false` means to cancel the previous shutdown request.

### Immediate Shutdown

> [!Note]
> This is part of the game automation API, which will only be used for games that Neuro can launch by herself.  
> As such, most games will not need to implement this.

This message will be sent when the game needs to be shutdown immediately. You have only a handful of seconds to save as much progress as possible. After you have saved, you can send back a shutdown ready message.

> [!Important]
> Please don't actually close the game, just save the current progress that can be saved.  
> Neuro will close the game herself.

```ts
{
    "command": "shutdown/immediate"
}
```

## Outgoing Messages (C2S, Game to Neuro)

### Shutdown Ready

> [!Note]
> This is part of the game automation API, which will only be used for games that Neuro can launch by herself.  
> As such, most games will not need to implement this.

This message should be sent as a response to a graceful or an imminent shutdown request, after progress has been saved. After this is sent, Neuro will close the game herself by terminating the process, so to reiterate you must definitely ensure that progress has already been saved. 

```ts
{
    "command": "shutdown/ready",
    "game": string
}
```
