export interface FeishuEvent {
  type: string;
  event: {
    message: {
      message_type: string;
      content: string;
    };
    sender: {
      sender_id: { open_id: string };
    };
  };
}

export interface FeishuChallenge {
  challenge: string;
  token: string;
  type: "url_verification";
}
