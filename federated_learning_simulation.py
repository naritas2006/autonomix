import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_score, recall_score, f1_score, accuracy_score
import random
import json
from collections import OrderedDict

# -----------------------------
# 1. Blockchain Export + Load
# -----------------------------

def export_verified_events(events, filename="verified_events.json"):
    with open(filename, "w") as f:
        json.dump(events, f, indent=4)


def simulate_and_export_data(num_events, num_zones, filename="verified_events.json"):
    events = []
    event_types = ["speeding", "harsh_braking", "sharp_turn", "accident"]

    for i in range(num_events):
        base_accident_prob = 0.15

        if i > 10:
            recent_events = events[i - 10:i]
            severe = [e for e in recent_events if e["severity"] > 0.7 and e["event_type"] != "accident"]
            if len(severe) > 1:
                base_accident_prob = 0.6

        is_accident = random.random() < base_accident_prob
        event_type = "accident" if is_accident else random.choice(event_types[:-1])

        event = {
            "zone_id": random.randint(0, num_zones - 1),
            "event_type": event_type,
            "timestamp": i * 60,
            "severity": random.uniform(0.8, 1.0) if event_type == "accident" else random.uniform(0.1, 0.7)
        }

        events.append(event)

    events = sorted(events, key=lambda x: x["timestamp"])
    export_verified_events(events, filename)
    return events


def load_event_data(filename="verified_events.json"):
    with open(filename, "r") as f:
        return json.load(f)


# -----------------------------
# 2. Feature Engineering
# -----------------------------

def feature_engineering(events, window_size_minutes, prediction_horizon_minutes):
    features = []
    labels = []

    window_sec = window_size_minutes * 60
    horizon_sec = prediction_horizon_minutes * 60

    event_types = sorted(list(set(e["event_type"] for e in events)))

    for i in range(len(events)):
        current = events[i]
        current_time = current["timestamp"]

        window_start = current_time - window_sec

        past_events = [
            e for e in events[:i]
            if e["timestamp"] >= window_start
            and e["timestamp"] < current_time
            and e["zone_id"] == current["zone_id"]
        ]

        if len(past_events) == 0:
            continue

        event_count = len(past_events)

        type_counts = {etype: 0 for etype in event_types}
        for e in past_events:
            type_counts[e["event_type"]] += 1

        type_freq = [type_counts[etype] / event_count for etype in event_types]

        time_of_day = (current_time % (24 * 3600)) / (24 * 3600)

        feature_vector = [event_count, time_of_day] + type_freq

        future_events = [
            e for e in events[i + 1:]
            if e["timestamp"] <= current_time + horizon_sec
            and e["zone_id"] == current["zone_id"]
        ]

        label = 1 if any(e["event_type"] == "accident" for e in future_events) else 0

        features.append(feature_vector)
        labels.append(label)

    return np.array(features), np.array(labels)


# -----------------------------
# 3. Non-IID Zone Split
# -----------------------------

def split_data_non_iid(features, labels, events, num_clients, num_zones):
    zones_per_client = num_zones // num_clients
    client_zone_map = {
        i: list(range(i * zones_per_client, (i + 1) * zones_per_client))
        for i in range(num_clients)
    }

    client_data = {i: {"features": [], "labels": []} for i in range(num_clients)}

    for idx in range(len(features)):
        zone = events[idx]["zone_id"]
        for client_id, zones in client_zone_map.items():
            if zone in zones:
                client_data[client_id]["features"].append(features[idx])
                client_data[client_id]["labels"].append(labels[idx])
                break

    for cid in client_data:
        client_data[cid]["features"] = np.array(client_data[cid]["features"])
        client_data[cid]["labels"] = np.array(client_data[cid]["labels"])

    return client_data


# -----------------------------
# 4. Model (No Sigmoid)
# -----------------------------

class RiskModel(nn.Module):
    def __init__(self, input_dim):
        super().__init__()
        self.linear = nn.Linear(input_dim, 1)

    def forward(self, x):
        return self.linear(x)  # logits


# -----------------------------
# 5. FL Client
# -----------------------------

class FLClient:
    def __init__(self, client_id, model, train_loader, test_loader):
        self.client_id = client_id
        self.model = model
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.optimizer = optim.Adam(self.model.parameters(), lr=0.01)

        y_train = train_loader.dataset.tensors[1]
        pos_weight = (len(y_train) - y_train.sum()) / (y_train.sum() + 1e-6)
        self.criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    def train(self, epochs):
        self.model.train()
        for _ in range(epochs):
            for x, y in self.train_loader:
                self.optimizer.zero_grad()
                logits = self.model(x)
                loss = self.criterion(logits, y)
                loss.backward()
                self.optimizer.step()

    def get_weights(self):
        return self.model.state_dict()

    def set_weights(self, weights):
        self.model.load_state_dict(weights)


# -----------------------------
# 6. Federated Averaging
# -----------------------------

def federated_averaging(client_weights):
    avg_weights = OrderedDict()
    for key in client_weights[0].keys():
        avg_weights[key] = sum(w[key] for w in client_weights) / len(client_weights)
    return avg_weights


# -----------------------------
# 7. Centralized Baseline
# -----------------------------

def train_centralized(features, labels, input_dim, epochs):
    X_train, X_test, y_train, y_test = train_test_split(
        features, labels, test_size=0.2, stratify=labels, random_state=42
    )

    train_data = TensorDataset(
        torch.tensor(X_train, dtype=torch.float32),
        torch.tensor(y_train, dtype=torch.float32).view(-1, 1),
    )

    test_data = TensorDataset(
        torch.tensor(X_test, dtype=torch.float32),
        torch.tensor(y_test, dtype=torch.float32).view(-1, 1),
    )

    train_loader = DataLoader(train_data, batch_size=32, shuffle=True)
    test_loader = DataLoader(test_data, batch_size=32)

    model = RiskModel(input_dim)
    optimizer = optim.Adam(model.parameters(), lr=0.01)

    pos_weight = (len(y_train) - sum(y_train)) / (sum(y_train) + 1e-6)
    criterion = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([pos_weight]))

    for _ in range(epochs):
        for x, y in train_loader:
            optimizer.zero_grad()
            logits = model(x)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()

    model.eval()
    all_true, all_pred = [], []

    with torch.no_grad():
        for x, y in test_loader:
            logits = model(x)
            probs = torch.sigmoid(logits)
            preds = (probs > 0.5).float()
            all_true.extend(y.numpy())
            all_pred.extend(preds.numpy())

    return (
        accuracy_score(all_true, all_pred),
        precision_score(all_true, all_pred),
        recall_score(all_true, all_pred),
        f1_score(all_true, all_pred),
    )


# -----------------------------
# 8. Main
# -----------------------------

def main():
    NUM_CLIENTS = 3
    NUM_ROUNDS = 5
    LOCAL_EPOCHS = 5
    NUM_EVENTS = 2000
    NUM_ZONES = 6
    WINDOW_SIZE = 30
    HORIZON = 10

    simulate_and_export_data(NUM_EVENTS, NUM_ZONES)

    events = load_event_data()
    features, labels = feature_engineering(events, WINDOW_SIZE, HORIZON)

    print("Total samples:", len(labels))
    print("Accident ratio:", sum(labels) / len(labels))

    input_dim = features.shape[1]

    # Federated
    client_data = split_data_non_iid(features, labels, events, NUM_CLIENTS, NUM_ZONES)

    global_model = RiskModel(input_dim)
    clients = []

    for i in range(NUM_CLIENTS):
        X = client_data[i]["features"]
        y = client_data[i]["labels"]

        if len(X) < 10:
            continue

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, stratify=y, random_state=42
        )

        train_loader = DataLoader(
            TensorDataset(torch.tensor(X_train, dtype=torch.float32),
                          torch.tensor(y_train, dtype=torch.float32).view(-1, 1)),
            batch_size=32, shuffle=True)

        test_loader = DataLoader(
            TensorDataset(torch.tensor(X_test, dtype=torch.float32),
                          torch.tensor(y_test, dtype=torch.float32).view(-1, 1)),
            batch_size=32)

        clients.append(FLClient(i, RiskModel(input_dim), train_loader, test_loader))

    # FL Rounds
    for r in range(NUM_ROUNDS):
        client_weights = []

        for client in clients:
            client.set_weights(global_model.state_dict())
            client.train(LOCAL_EPOCHS)
            client_weights.append(client.get_weights())

        global_weights = federated_averaging(client_weights)
        global_model.load_state_dict(global_weights)

    # Evaluate FL
    all_true, all_pred = [], []

    for client in clients:
        client.set_weights(global_model.state_dict())
        client.model.eval()
        with torch.no_grad():
            for x, y in client.test_loader:
                logits = client.model(x)
                probs = torch.sigmoid(logits)
                preds = (probs > 0.5).float()
                all_true.extend(y.numpy())
                all_pred.extend(preds.numpy())

    fed_results = (
        accuracy_score(all_true, all_pred),
        precision_score(all_true, all_pred),
        recall_score(all_true, all_pred),
        f1_score(all_true, all_pred),
    )

    # Centralized
    cen_results = train_centralized(features, labels, input_dim, NUM_ROUNDS * LOCAL_EPOCHS)

    print("\n--- Final Model Comparison ---")
    print("Federated:", fed_results)
    print("Centralized:", cen_results)


if __name__ == "__main__":
    main()