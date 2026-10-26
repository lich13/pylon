//! Bounded tails for process output; appending never shifts the retained buffer.
use std::collections::VecDeque;

pub(crate) struct Tail {
    bytes: VecDeque<u8>,
    limit: usize,
}

impl Tail {
    pub fn new(limit: usize) -> Self {
        Self {
            bytes: VecDeque::new(),
            limit,
        }
    }

    pub fn append(&mut self, bytes: &[u8]) {
        let bytes = &bytes[bytes.len().saturating_sub(self.limit)..];
        let discard = (self.bytes.len() + bytes.len()).saturating_sub(self.limit);
        self.bytes.drain(..discard);
        self.bytes.extend(bytes);
    }

    pub fn text(&mut self) -> String {
        String::from_utf8_lossy(self.bytes.make_contiguous()).into_owned()
    }

    pub fn last(&self, limit: usize) -> Vec<u8> {
        self.bytes
            .iter()
            .skip(self.bytes.len().saturating_sub(limit))
            .copied()
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrap_large_chunks_and_utf8_match_the_original_tail() {
        let mut tail = Tail::new(13);
        let mut reference = Vec::new();
        for bytes in [
            b"initial".as_slice(),
            "中文长输出".as_bytes(),
            b"01234567890123456789",
            b"end",
        ] {
            tail.append(bytes);
            reference.extend_from_slice(bytes);
            reference.drain(..reference.len().saturating_sub(13));
            assert_eq!(tail.text(), String::from_utf8_lossy(&reference));
            assert_eq!(tail.last(4), reference[reference.len().saturating_sub(4)..]);
        }
    }
}
