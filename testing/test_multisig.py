# (c) Copyright 2018 by Coinkite Inc. This file is part of Coldcard <coldcardwallet.com>
# and is covered by GPLv3 license found in COPYING.
#
# Multisig-related tests.
#
import time, pytest, os, random
#from psbt import BasicPSBT, BasicPSBTInput, BasicPSBTOutput, PSBT_IN_REDEEM_SCRIPT
from ckcc.protocol import CCProtocolPacker, CCProtoError, MAX_TXN_LEN, CCUserRefused
from pprint import pprint, pformat
from base64 import b64encode, b64decode
from helpers import B2A, U2SAT, prandom
from ckcc_protocol.constants import AF_P2WSH, AFC_SCRIPT, AF_P2SH, AF_P2WSH_P2SH
from struct import unpack, pack
from conftest import simulator_fixed_xprv, simulator_fixed_xfp
from pycoin.key.BIP32Node import BIP32Node

def xfp2str(xfp):
    # Standardized way to show an xpub's fingerprint... it's a 4-byte string
    # and not really an integer. Used to show as '0x%08x' but that's wrong endian.
    from binascii import b2a_hex
    return b2a_hex(pack('>I', xfp)).decode('ascii').upper()

def HARD(n=0):
    return 0x80000000 | n

unmap_addr_fmt = {
    'p2sh': AF_P2SH,
    'p2wsh': AF_P2WSH,
    'p2wsh-p2sh': AF_P2WSH_P2SH,
}

@pytest.fixture()
def bitcoind_p2sh(bitcoind):
    # Use bitcoind to generate a p2sh addres based on public keys.

    def doit(M, pubkeys, fmt):

        fmt = {
            AF_P2SH: 'legacy',
            AF_P2WSH: 'bech32',
            AF_P2WSH_P2SH: 'p2sh-segwit'
        }[fmt]

        try:
            rv = bitcoind.createmultisig(M, [B2A(i) for i in pubkeys], fmt)
        except ConnectionResetError:
            # bitcoind sleeps on us sometimes, give it another chance.
            rv = bitcoind.createmultisig(M, [B2A(i) for i in pubkeys], fmt)

        return rv['address'], rv['redeemScript']

    return doit

@pytest.fixture
def clear_ms(unit_test):
    def doit():
        unit_test('devtest/wipe_ms.py')
    return doit

@pytest.fixture()
def make_multisig():
    # make a multsig wallet, always with simulator as an element

    # always BIP45:   m/45'/...

    def doit(M, N):
        keys = {}

        for i in range(N-1):
            pk = BIP32Node.from_master_secret(b'CSW is a fraud %d' % i, 'XTN')

            xfp = unpack("<I", pk.fingerprint())[0]

            sub = pk.subkey(45, is_hardened=True, as_private=True)
            keys[xfp] = pk, sub

        pk = BIP32Node.from_wallet_key(simulator_fixed_xprv)
        keys[simulator_fixed_xfp] = pk, pk.subkey(45, is_hardened=True, as_private=True)

        return keys

    return doit

@pytest.fixture
def offer_import(cap_story, dev):
    def doit(config):
        # upload the file, trigger import
        file_len, sha = dev.upload_file(config.encode('ascii'))

        dev.send_recv(CCProtocolPacker.multisig_enroll(file_len, sha))

        time.sleep(.2)
        title, story = cap_story()
        #print(repr(story))

        return title, story

    return doit

@pytest.fixture
def import_ms_wallet(dev, make_multisig, offer_import):

    def doit(M, N, addr_fmt=None, name=None):
        keys = make_multisig(M, N)

        # render as a file for import
        name = name or f'test-{M}-{N}'
        config = f"name: {name}\npolicy: {M} / {N}\n\n"

        if addr_fmt:
            config += f'format: {addr_fmt.upper()}\n'

        config += '\n'.join('%s: %s' % (xfp2str(k), dd.hwif(as_private=False)) 
                                            for k, (m, dd) in keys.items())
        #print(config)

        title, story = offer_import(config)

        assert 'Create new multisig' in story
        assert name in story
        assert f'Policy: {M} of {N}\n' in story

        return keys

    return doit


@pytest.mark.parametrize('N', [ 3, 15])
def test_ms_import_variations(N, make_multisig, clear_ms, offer_import, need_keypress):
    # all the different ways...
    keys = make_multisig(N, N)

    # bare, no fingerprints
    # - no xfps
    # - no meta data
    config = '\n'.join(sk.hwif(as_private=False) for m,sk in keys.values())
    title, story = offer_import(config)
    assert f'Policy: {N} of {N}\n' in story
    need_keypress('x')

    # exclude myself (expect fail)
    config = '\n'.join(sk.hwif(as_private=False) 
                            for xfp,(m,sk) in keys.items() if xfp != simulator_fixed_xfp)

    with pytest.raises(BaseException) as ee:
        title, story = offer_import(config)
    assert 'my key not included' in str(ee.value)


    # normal names
    for name in [ 'Zy', 'Z'*20 ]:
        config = f'name: {name}\n'
        config += '\n'.join(sk.hwif(as_private=False) for m,sk in keys.values())
        title, story = offer_import(config)
        need_keypress('x')
        assert name in story

    # too long name
    config = 'name: ' + ('A'*21) + '\n'
    config += '\n'.join(sk.hwif(as_private=False) for m,sk in keys.values())
    with pytest.raises(BaseException) as ee:
        title, story = offer_import(config)
    assert '20 long' in str(ee.value)

    # comments, blank lines
    config = [sk.hwif(as_private=False) for m,sk in keys.values()]
    for i in range(len(config)):
        config.insert(i, '# comment')
        config.insert(i, '')
    title, story = offer_import('\n'.join(config))
    assert f'Policy: {N} of {N}\n' in story
    need_keypress('x')

    # the different addr formats
    for af in unmap_addr_fmt.keys():
        config = f'format: {af}\n'
        config += '\n'.join(sk.hwif(as_private=False) for m,sk in keys.values())
        title, story = offer_import(config)
        need_keypress('x')
        assert f'Policy: {N} of {N}\n' in story

def make_redeem(M, keys, paths):
    N = len(keys)

    # see BIP 67: <https://github.com/bitcoin/bips/blob/master/bip-0067.mediawiki>

    pubkeys = []
    for xfp in keys:
        node = keys[xfp][0]     # master root key
        path = paths[xfp]

        #print(xfp2str(xfp),end=': ')

        for p in path:
            node = node.subkey(p & ~0x80000000, is_hardened=bool(p & 0x80000000))
            if p == 2147483693:
                assert node == keys[xfp][1]

        pk = node.sec(use_uncompressed=False)
        pubkeys.append(pk)
        #print(f"{xfp2str(xfp)} {path} => {B2A(pk)}")

    pubkeys.sort()

    mm = [80 + M] if M <= 16 else [1, M]
    nn = [80 + N] if N <= 16 else [1, N]

    rv = bytes(mm)

    for pk in pubkeys:
        rv += bytes([len(pk)]) + pk

    rv += bytes(nn + [0xAE])

    print("redeem script: " + B2A(rv))

    return rv, pubkeys
        
    

@pytest.fixture
def test_ms_show_addr(dev, cap_story, need_keypress, addr_vs_path, bitcoind_p2sh):
    def doit(M, keys, subpath=[1,2,3], addr_fmt=AF_P2SH, give_scr=True):
        # test we are showing addresses correctly
        addr_fmt = unmap_addr_fmt.get(addr_fmt, addr_fmt)

        # limitation: assume BIP45 here, but don't do cosigner index
        paths = [[xfp, HARD(45)] + subpath for xfp in keys]

        # pre-calc redeem script
        #print(repr(paths))
        scr, pubkeys = make_redeem(M, keys, dict((a,b) for a,*b in paths))
        assert len(scr) <= 520, "script too long for standard!"

        got_addr = dev.send_recv(CCProtocolPacker.show_p2sh_address(
                                    M, paths, addr_fmt, scr if give_scr else b''),
                                    timeout=None)

        title, story = cap_story()

        #print(story)

        assert got_addr in story
        assert all((xfp2str(i) in story) for i in keys)
        assert '/?/'+'/'.join(str(i) for i in subpath) in story

        need_keypress('y')
        # check expected addr was generated based on my math
        addr_vs_path(got_addr, addr_fmt=addr_fmt, script=scr)

        # also check against bitcoind
        core_addr, core_scr = bitcoind_p2sh(M, pubkeys, addr_fmt)
        assert B2A(scr) == core_scr
        assert core_addr == got_addr


    return doit
    

@pytest.mark.parametrize('m_of_n', [(1,3), (2,3), (3,3), (3,6), (10, 15), (15,15)])
@pytest.mark.parametrize('addr_fmt', ['p2wsh-p2sh', 'p2sh', 'p2wsh' ])
@pytest.mark.parametrize('give_scr', [False, True])
def test_import_ranges(m_of_n, addr_fmt, clear_ms, import_ms_wallet, need_keypress, test_ms_show_addr, give_scr):

    M, N = m_of_n

    #if addr_fmt == 'p2wsh-p2sh':
        #raise pytest.xfail('not done')

    keys = import_ms_wallet(M, N, addr_fmt)

    time.sleep(.1)
    need_keypress('y')

    # test an address that should be in that wallet.
    time.sleep(.1)
    test_ms_show_addr(M, keys, addr_fmt=addr_fmt, give_scr=give_scr)

    # cleanup
    clear_ms()

def test_import_detail(clear_ms, import_ms_wallet, need_keypress, cap_story):
    # check all details are shown right

    M,N = 14, 15

    keys = import_ms_wallet(M, N)

    time.sleep(.2)
    need_keypress('1')

    time.sleep(.1)
    title, story = cap_story()

    assert title == f'{M} of {N}'
    xpubs = [b.hwif() for a,b in keys.values()]
    for xp in xpubs:
        assert xp in story

    need_keypress('x')

    time.sleep(.1)
    need_keypress('x')


def test_export_bip45_multisig(goto_home, cap_story, pick_menu_item, cap_menu, need_keypress, microsd_path):
    # test UX and math for bip45 export

    goto_home()
    pick_menu_item('Settings')
    pick_menu_item('Multisig Wallets')
    pick_menu_item('BIP45 Export')

    time.sleep(.1)
    title, story = cap_story()
    assert 'BIP45' in title
    assert 'BIP45' in story
    assert "m/45'" in story
    
    need_keypress('y')

    time.sleep(.1)
    title, story = cap_story()
    fname = story.split('\n')[-1]

    with open(microsd_path(fname), 'rt') as fp:
        xpub = fp.read().strip()

        n = BIP32Node.from_wallet_key(xpub)

    assert n.tree_depth() == 1
    assert n.child_index() == 45 | (1<<31)
    mxfp = unpack("<I", n.parent_fingerprint())[0]
    assert hex(mxfp) == hex(simulator_fixed_xfp)

    e = BIP32Node.from_wallet_key(simulator_fixed_xprv)
    expect = e.subkey_for_path("45'.pub") 
    assert expect.hwif() == n.hwif()

@pytest.mark.parametrize('N', [ 3, 15])
def test_import_ux(N, goto_home, cap_story, pick_menu_item, cap_menu, need_keypress, microsd_path, make_multisig):
    # test menu-based UX for importing wallet file from SD
    M = N-1

    keys = make_multisig(M, N)
    name = 'named-%d' % random.randint(10000,99999)
    config = f'policy: {M} of {N}\n'
    config += '\n'.join(sk.hwif(as_private=False) for m,sk in keys.values())

    fname = microsd_path(f'ms-{name}.txt')
    with open(fname, 'wt') as fp:
        fp.write(config)

    try:
        goto_home()
        pick_menu_item('Settings')
        pick_menu_item('Multisig Wallets')
        pick_menu_item('Import from SD')

        time.sleep(.1)
        _, story = cap_story()
        assert "Pick file" in story
        need_keypress('y')

        time.sleep(.1)
        pick_menu_item(fname.rsplit('/', 1)[1])

        time.sleep(.1)
        _, story = cap_story()

        assert 'Create new multisig' in story
        assert name in story, 'didnt infer wallet name from filename'
        assert f'Policy: {M} of {N}\n' in story

        # abort install
        need_keypress('x')

    finally:
        # cleanup
        try: os.unlink(fname)
        except: pass
    
@pytest.mark.parametrize('addr_fmt', ['p2wsh-p2sh', 'p2sh', 'p2wsh' ])
def test_export_single_ux(goto_home, cap_story, pick_menu_item, cap_menu, need_keypress, microsd_path, import_ms_wallet, addr_fmt, clear_ms):

    # create a wallet, export to SD card, check file created.

    clear_ms()

    name = 'ex-test-%d' % random.randint(10000,99999)
    M,N = 3, 15
    keys = import_ms_wallet(M, N, name=name, addr_fmt=addr_fmt)
    time.sleep(.1)
    need_keypress('y')

    goto_home()
    pick_menu_item('Settings')
    pick_menu_item('Multisig Wallets')

    menu = cap_menu()
    item = [i for i in menu if name in i][0]
    pick_menu_item(item)

    time.sleep(.1)
    need_keypress('1')

    time.sleep(.1)
    title, story = cap_story()
    fname = story.split('\n')[-1]

    try:
        got = set()
        with open(microsd_path(fname), 'rt') as fp:
            for ln in fp.readlines():
                ln = ln.strip()
                if '#' in ln:
                    assert ln[0] == '#'
                    continue
                if not ln:
                    continue

                assert ':' in ln
                label, value = ln.split(': ')

                if label == 'name':
                    assert value == name
                    got.add(label)
                elif label == 'policy':
                    assert value == f'{M} of {N}'
                    got.add(label)
                elif label == 'format':
                    assert value == addr_fmt
                    assert addr_fmt != 'p2sh'
                    got.add(label)
                else:
                    assert len(label) == 8
                    xfp = int(label, 16)
                    got.add(xfp)
                    assert xfp in keys
                    n = BIP32Node.from_wallet_key(value)

        if 'format' not in got:
            assert addr_fmt == 'p2sh'
            got.add('format')

        assert len(got) == 3 + N

        time.sleep(.1)
        need_keypress('y')
    finally:
        try: os.unlink(fname)
        except: pass

    # test delete while we're here
    time.sleep(.1)
    pick_menu_item(item)

    time.sleep(.1)
    need_keypress('6')

    time.sleep(.2)
    _, story = cap_story()
    assert 'you SURE' in story
    assert name in story

    need_keypress('y')
    time.sleep(.1)
    menu = cap_menu()
    assert not [i for i in menu if name in i]
    assert '(none setup yet)' in menu


@pytest.mark.parametrize('N', [ 3, 15])
def test_overflow(N, import_ms_wallet, clear_ms, need_keypress, cap_story):
    clear_ms()
    M = N
    name = 'a'*20       # longest possible
    for count in range(1, 10):
        keys = import_ms_wallet(M, N, name=name, addr_fmt='p2wsh')
        time.sleep(.1)
        need_keypress('y')

        time.sleep(.2)
        title, story = cap_story()
        if title or story:
            print(f'Failed with {count} @ {N} keys each')
            assert 'No space left' in story
            break

    if N == 3:
        assert count == 9, "Expect fail at 9"
    if N == 15:
        assert count == 2, "Expect fail at 2"

    need_keypress('y')
    clear_ms()

@pytest.mark.parametrize('N', [ 3, 15])
def test_make_example_file(N, microsd_path, make_multisig, addr_fmt=None):
    M=3
    keys = make_multisig(M, N)

    # render as a file for import
    name = f'sample-{M}-{N}'
    config = f"name: {name}\npolicy: {M} / {N}\n\n"

    if addr_fmt:
        config += f'format: {addr_fmt.upper()}\n'

    config += '\n'.join('%s: %s' % (xfp2str(k), dd.hwif(as_private=False)) 
                                        for k, (m, dd) in keys.items())

    fname = microsd_path(f'{name}.txt')
    with open(fname, 'wt') as fp:
        fp.write(config+'\n')

    print(f"Created: {fname}")

# EOF
