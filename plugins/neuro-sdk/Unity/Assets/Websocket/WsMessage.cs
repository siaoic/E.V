#nullable enable

using Newtonsoft.Json;

namespace NeuroSdk.Websocket
{
    public record WsMessage
    {
        public WsMessage(string command, object? data, string game)
        {
            Command = command;
            _game = game;
            Data = data;
        }

        [JsonProperty("command", Order = 0)]
        public readonly string Command;

        [JsonProperty("game", Order = 10)]
        private readonly string _game;

        [JsonProperty("data", Order = 20)]
        public readonly object? Data;
    }
}
