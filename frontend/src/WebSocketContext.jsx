import React from 'react';
import _ from 'lodash';
import { meanSquaredError, meanAbsoluteError } from './Util';

const WebSocketContext = React.createContext(null);
const GameInfoContext = React.createContext(null);

const WebSocketProvider = ({ children }) => {
  const [connectionStatus, setConnectionStatus] = React.useState('disconnected');
  const [webSocketAddress, setWebSocketAddress] = React.useState(null);
  const [webSocket, setWebSocket] = React.useState(null);

  const [sorting, setSorting] = React.useState([]);
  const [lossHistory, setLossHistory] = React.useState([]);
  const [gameInfo, setGameInfo] = React.useState({});
  const [trainingPlayerCount, setTrainingPlayerCount] = React.useState(4);

  const [configuration, setConfiguration] = React.useState({});

  const handleLossBatch = (data) => {
    const newData = getGameInfo(data);
    setGameInfo(prevGameInfo => ({...prevGameInfo, ...newData}));
  }

  const handleTrainingEpoch = (data) => {
    setLossHistory(prevLossHistory => [...prevLossHistory, data.loss]);
    const newData = getGameInfo(data);
    setGameInfo(prevGameInfo => ({...prevGameInfo, ...newData}));
  };

  const handleTrainingStart = (data) => {
    if (isNaN(data.player_count)) {
      console.log(`Data did not contain numerical player count ${data}`)
    }
    setTrainingPlayerCount(data.player_count);
  }

  const makeWebsocketRequest = (type, data) => {
    if (webSocket && webSocket.readyState === WebSocket.OPEN) {
      webSocket.send(JSON.stringify({ type, data }));
    }
  }

  const messageTypeToHandler = {
    "training_epoch": handleTrainingEpoch,
    "training_start": handleTrainingStart,
    "loss_batch": handleLossBatch,
  };

  const processGameData = (data, uuid, tracker_suffixes, y, y_pred, masks, y_loss) => {
    let maximizer = _.maxBy(
      _.zip(y, y_pred, masks, [...Array(y_pred.length).keys()]),
      (values) => Math.abs(values[0] - values[1]) * values[2]
    )[3]

    let players = _.zipWith(
      tracker_suffixes, y, y_pred, masks,
      (tracker_suffix, mmr, prediction, mask) => {
        return {tracker_suffix, mmr, prediction, mask};
      }
    )
    players[maximizer].isBiggestMiss = true;

    return [uuid, {
      "uuid": uuid,
      players,
      y_pred,
      y,
      masks,
      y_loss,
      RMSE: Math.sqrt(meanSquaredError(y, y_pred, masks)),
      MAE: meanAbsoluteError(y, y_pred, masks),
      "update_epoch": data.epoch,
    }]
  }

  const getGameInfo = (data) => {
    const zipped = _.zip(
      data.uuids, data.tracker_suffixes, data.y,
      data.y_pred, data.mask, data.y_loss
    );
    return Object.fromEntries(zipped.map((args) => processGameData(data, ...args)));
  }

  React.useEffect(() => {
    if (!webSocketAddress) {
      return;
    }

    let socket = new WebSocket(webSocketAddress)
    setWebSocket(socket);

    socket.onopen = () => {
      setConnectionStatus('connected');
    };

    socket.onerror = () => {
      setConnectionStatus('error');
    };

    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type in messageTypeToHandler) {
        messageTypeToHandler[message.type](message.data);
      } else {
        console.log(`Unable to handle message of type ${message.type}`)
      }
    };

	socket.onclose = () => {
	  setConnectionStatus('disconnected');
      setWebSocketAddress(null);
	};

    return () => {
      socket.close();
      setWebSocketAddress(null);
    };

  }, [webSocketAddress]);

  return (
	<WebSocketContext.Provider value={{ lossHistory, gameInfo, connectionStatus, setWebSocketAddress, webSocket, trainingPlayerCount, makeWebsocketRequest, sorting, setSorting }}>
      <GameInfoContext.Provider value={{ gameInfo }}>
        {children}
      </GameInfoContext.Provider >
    </WebSocketContext.Provider>
  );
};

export { GameInfoContext, WebSocketContext, WebSocketProvider };
