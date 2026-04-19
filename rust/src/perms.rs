use crate::errors::{Error, Result};
use crate::types::Access;

pub fn check_access(
    node_mode: u32, node_uid: u32, node_gid: u32,
    caller_uid: u32, caller_gid: u32,
    access: Access,
) -> bool {
    if caller_uid == 0 {
        if access.contains(Access::X) {
            let any_x = (node_mode & 0o111) != 0;
            if !any_x { return false; }
        }
        return true;
    }

    let bits: u32 = if caller_uid == node_uid {
        (node_mode >> 6) & 0o7
    } else if caller_gid == node_gid {
        (node_mode >> 3) & 0o7
    } else {
        node_mode & 0o7
    };

    let required = access.bits();
    (bits & required) == required
}

pub fn require_access(
    node_mode: u32, node_uid: u32, node_gid: u32,
    caller_uid: u32, caller_gid: u32,
    access: Access,
) -> Result<()> {
    if check_access(node_mode, node_uid, node_gid, caller_uid, caller_gid, access) {
        Ok(())
    } else {
        Err(Error::PermissionDenied(format!(
            "access {access:?} denied: caller=({caller_uid},{caller_gid}), \
             node=({node_uid},{node_gid},0o{node_mode:o})"
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owner_bits_used_for_owner_caller() {
        assert!(check_access(0o700, 1000, 1000, 1000, 1000, Access::R));
        assert!(check_access(0o700, 1000, 1000, 1000, 1000, Access::R | Access::W | Access::X));
    }

    #[test]
    fn group_bits_used_for_group_caller() {
        assert!(check_access(0o070, 1000, 1000, 2000, 1000, Access::R));
        assert!(check_access(0o070, 1000, 1000, 2000, 1000, Access::W));
    }

    #[test]
    fn other_bits_used_for_unrelated_caller() {
        assert!(check_access(0o007, 1000, 1000, 2000, 2000, Access::R | Access::W | Access::X));
        assert!(!check_access(0o070, 1000, 1000, 2000, 2000, Access::R));
    }

    #[test]
    fn root_has_rw_regardless_of_mode() {
        assert!(check_access(0o000, 1000, 1000, 0, 0, Access::R));
        assert!(check_access(0o000, 1000, 1000, 0, 0, Access::W));
        assert!(check_access(0o000, 1000, 1000, 0, 0, Access::R | Access::W));
    }

    #[test]
    fn root_execute_requires_at_least_one_x_bit() {
        assert!(!check_access(0o666, 1000, 1000, 0, 0, Access::X));
        assert!(check_access(0o100, 1000, 1000, 0, 0, Access::X));
        assert!(check_access(0o010, 1000, 1000, 0, 0, Access::X));
        assert!(check_access(0o001, 1000, 1000, 0, 0, Access::X));
    }

    #[test]
    fn combined_access_requires_all_bits() {
        assert!(check_access(0o400, 1000, 1000, 1000, 1000, Access::R));
        assert!(!check_access(0o400, 1000, 1000, 1000, 1000, Access::W));
        assert!(!check_access(0o400, 1000, 1000, 1000, 1000, Access::R | Access::W));
    }

    #[test]
    fn require_access_raises_on_deny() {
        let err = require_access(0o000, 1000, 1000, 2000, 2000, Access::R).unwrap_err();
        assert!(matches!(err, Error::PermissionDenied(_)));
        assert!(require_access(0o700, 1000, 1000, 1000, 1000, Access::R).is_ok());
    }
}
