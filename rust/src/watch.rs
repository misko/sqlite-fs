use std::sync::{Arc, Mutex};
use std::sync::mpsc::{channel, Receiver, Sender, TryRecvError};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::types::{Event, EventKind};

#[derive(Debug, Clone, Copy, Default)]
pub struct WatchMask;

pub struct Watcher {
    rx: Receiver<Event>,
    slot_id: u64,
    registry: Arc<Mutex<WatchRegistry>>,
}

impl Watcher {
    pub fn try_recv(&self) -> Option<Event> {
        match self.rx.try_recv() {
            Ok(ev)                          => Some(ev),
            Err(TryRecvError::Empty)        => None,
            Err(TryRecvError::Disconnected) => None,
        }
    }

    pub fn recv(&self) -> Option<Event> {
        self.rx.recv().ok()
    }
}

impl Drop for Watcher {
    fn drop(&mut self) {
        if let Ok(mut reg) = self.registry.lock() {
            reg.slots.retain(|s| s.id != self.slot_id);
        }
    }
}

pub(crate) struct WatchSlot {
    pub id:        u64,
    pub path:      String,
    pub recursive: bool,
    pub tx:        Sender<Event>,
}

pub(crate) struct WatchRegistry {
    next_id: u64,
    pub(crate) slots: Vec<WatchSlot>,
}

impl WatchRegistry {
    pub fn new() -> Self { Self { next_id: 1, slots: Vec::new() } }

    pub fn subscribe(
        registry: &Arc<Mutex<Self>>, path: String, recursive: bool,
    ) -> Watcher {
        let (tx, rx) = channel();
        let mut reg = registry.lock().expect("watch registry poisoned");
        let id = reg.next_id;
        reg.next_id += 1;
        reg.slots.push(WatchSlot { id, path, recursive, tx });
        Watcher { rx, slot_id: id, registry: Arc::clone(registry) }
    }

    pub fn emit(&self, ev: Event) {
        for slot in &self.slots {
            if !matches_slot(slot, &ev) { continue; }
            let _ = slot.tx.send(ev.clone());
        }
    }
}

fn matches_slot(slot: &WatchSlot, ev: &Event) -> bool {
    let paths: Vec<&str> = match ev.kind {
        EventKind::Move => {
            let mut v = Vec::new();
            if let Some(s) = ev.src_path.as_deref() { v.push(s); }
            if let Some(s) = ev.dst_path.as_deref() { v.push(s); }
            if v.is_empty() { v.push(&ev.path); }
            v
        }
        _ => vec![ev.path.as_str()],
    };
    paths.iter().any(|p| path_matches(p, &slot.path, slot.recursive))
}

fn path_matches(event_path: &str, watched: &str, recursive: bool) -> bool {
    let parent = parent_of_path(event_path);
    if recursive {
        event_path.starts_with(watched)
            && (event_path.len() == watched.len()
                || event_path.as_bytes().get(watched.len()) == Some(&b'/')
                || watched == "/")
            && event_path != watched
    } else {
        parent == watched
    }
}

fn parent_of_path(p: &str) -> &str {
    match p.rfind('/') {
        Some(0)   => "/",
        Some(idx) => &p[..idx],
        None      => "/",
    }
}

pub fn now_ns() -> i64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(d)  => d.as_nanos() as i64,
        Err(_) => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::NodeKind;

    fn ev(kind: EventKind, path: &str) -> Event {
        Event {
            kind, path: path.into(), src_path: None, dst_path: None,
            node_kind: NodeKind::File, inode: 1, timestamp_ns: 0,
        }
    }

    #[test]
    fn subscribe_and_emit_delivers_event() {
        let reg = Arc::new(Mutex::new(WatchRegistry::new()));
        let w = WatchRegistry::subscribe(&reg, "/data".into(), false);
        reg.lock().unwrap().emit(ev(EventKind::Create, "/data/x"));
        let got = w.try_recv().unwrap();
        assert_eq!(got.path, "/data/x");
    }

    #[test]
    fn non_recursive_misses_grandchildren() {
        let reg = Arc::new(Mutex::new(WatchRegistry::new()));
        let w = WatchRegistry::subscribe(&reg, "/data".into(), false);
        reg.lock().unwrap().emit(ev(EventKind::Create, "/data/sub/x"));
        assert!(w.try_recv().is_none());
    }

    #[test]
    fn recursive_sees_grandchildren() {
        let reg = Arc::new(Mutex::new(WatchRegistry::new()));
        let w = WatchRegistry::subscribe(&reg, "/data".into(), true);
        reg.lock().unwrap().emit(ev(EventKind::Create, "/data/sub/x"));
        assert_eq!(w.try_recv().unwrap().path, "/data/sub/x");
    }

    #[test]
    fn move_matches_src_or_dst() {
        let reg = Arc::new(Mutex::new(WatchRegistry::new()));
        let w = WatchRegistry::subscribe(&reg, "/a".into(), false);
        let move_ev = Event {
            kind: EventKind::Move, path: "/b/y".into(),
            src_path: Some("/a/x".into()), dst_path: Some("/b/y".into()),
            node_kind: NodeKind::File, inode: 1, timestamp_ns: 0,
        };
        reg.lock().unwrap().emit(move_ev);
        assert!(w.try_recv().is_some());
    }

    #[test]
    fn drop_unsubscribes() {
        let reg = Arc::new(Mutex::new(WatchRegistry::new()));
        {
            let _w = WatchRegistry::subscribe(&reg, "/data".into(), false);
            assert_eq!(reg.lock().unwrap().slots.len(), 1);
        }
        assert_eq!(reg.lock().unwrap().slots.len(), 0);
    }
}
