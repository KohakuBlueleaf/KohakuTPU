//! Exact key equality under deliberately colliding HashMap hashes.
use super::*;
use std::hash::{BuildHasherDefault, Hasher};

#[derive(Default)]
struct Collision;
impl Hasher for Collision {
    fn finish(&self) -> u64 {
        0
    }
    fn write(&mut self, _: &[u8]) {}
}

fn distinct<K: Clone + Eq + Hash>(keys: Vec<K>) {
    let mut map = HashMap::with_hasher(BuildHasherDefault::<Collision>::default());
    for (i, key) in keys.iter().enumerate() {
        assert!(map.insert(key.clone(), i).is_none());
    }
    for (i, key) in keys.iter().enumerate() {
        assert_eq!(map.get(key), Some(&i));
    }
    assert_eq!(map.len(), keys.len());
}

#[test]
fn collisions_never_alias_template_bound_or_machine_keys() {
    let base = TemplateCacheKey {
        package: Arc::from(&b"package-a"[..]),
        machine: Arc::from("machine-a"),
        mesh: 0,
        template: None,
        images: None,
    };
    let mut keys = vec![base.clone()];
    let mut k = base.clone();
    k.package = Arc::from(&b"package-b"[..]);
    keys.push(k);
    let mut k = base.clone();
    k.machine = Arc::from("machine-b");
    keys.push(k);
    let mut k = base.clone();
    k.template = Some(Arc::from("{}"));
    keys.push(k);
    let mut k = base.clone();
    k.images = Some(Arc::from("{}"));
    keys.push(k);
    let mut k = base;
    k.mesh = 1;
    keys.push(k);
    distinct(keys.clone());
    distinct(
        keys.into_iter()
            .map(|template| BoundCacheKey {
                template,
                translated: false,
                addresses: vec![1024],
                lower: false,
                after: None,
                params: Default::default(),
                count_path: 0,
            })
            .collect(),
    );
    distinct(vec![String::from("machine-a"), String::from("machine-b")]);
}
