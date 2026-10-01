"""Programs for the shared RTL/Sail platform behavior."""
import random
import struct

from verif.core.programs import i, r, store, branch, jal, csr, constant


def program(seed, self_modifying=False):
    boot = [i(31,0,256), csr(0,31,0x305,1), *constant(31,0x1800), csr(0,31,0x300,1)]
    boot += [jal(0,0x400-4*len(boot))]
    words = [i(reg,0,reg*13-512) for reg in range(1,32)]
    words += [i(1,0,-1), i(2,0,1)]
    for f3 in (0,1,4,5,6,7):
        for a,b in ((1,2),(2,1),(1,1)):
            words += [branch(a,b,8,f3), i(4,4,1)]
    words += [i(26,0,7), i(26,26,-1), branch(26,0,-4,1)]
    words += [jal(5,8), i(6,6,1), 0x00000397]
    target = 0x400+(len(words)+3)*4
    words += constant(5,target)+[i(5,5,1,0,0x67)]
    words += constant(28,0x8000)+constant(27,0x89abcdef)
    for size, offsets in ((0,range(4)),(1,(0,2)),(2,(0,))):
        for offset in offsets:
            words += [store(28,27,offset,size), i(9,28,offset,size,3)]
            if size < 2: words += [i(10,28,offset,size+4,3)]
    words += [i(0,28,0,2,3)]
    for base in (0x10000000,0x10001000):
        words += constant(28,base)
        for size, offset in ((0,1),(1,2),(2,4)):
            words += [store(28,27,offset,size), i(11,28,offset,size,3)]
    words += constant(28,0x8000)+[i(7,28,1,2,3), store(28,27,1,1)]
    words += constant(28,0x20000000)+[i(7,28,0,2,3), store(28,27,0,2)]
    words += [jal(5,2), branch(1,1,2), branch(1,2,2), i(5,0,2), i(5,5,1,0,0x67)]
    words += [0, 0x02001013, 0x02000033, 0x73, 0x100073, 0x10500073, 0xf, 0x100f]
    words += constant(16,0x12345678)
    for addr in (0x340,0x341):
        for f3 in (1,2,3,5,6,7):
            words += [csr(17,16,addr,f3), csr(18,0,addr,f3), csr(0,16,addr,f3)]
    for addr in (0x301,0x310,0xf11,0xf12,0xf13,0xf14,0xf15):
        words += [csr(19,0,addr,2)]
        if addr >= 0xf00: words += [csr(20,16,addr,1)]
    words += [csr(20,0,0xfff,2), csr(0,8,0x300,5), 0x73]
    words += constant(28,0x200)+constant(27,i(10,0,77))
    words += [store(28,27,0,2), 0x100f, i(1,28,0,0,0x67)]
    # The fetch-fault handler uses x29 as a return address outside the faulting region.
    resume = 0x400+(len(words)+5)*4
    words += constant(29,resume)+constant(28,0x10000000)+[i(0,28,0,0,0x67)]
    if self_modifying:
        # Replace already-fetched code with word/byte stores, then FENCE.I mid-line or at a line end.
        for size, offset, value, slot in ((2,0,i(12,0,0x2c3),3),(0,3,0xa5,3),(2,0,i(12,0,0x5a5),7)):
            while (len(words)+5) % 8 != slot: words.append(i(0,0,0))
            target = 0x400+4*(len(words)+6)
            words += constant(28,target)+constant(27,value)+[store(28,27,offset,size), 0x100f, i(12,0,0x111)]
    words += constant(28,0x8000)
    rng = random.Random(seed)
    for _ in range(600):
        rd,a,b = (rng.randrange(1,26),rng.randrange(26),rng.randrange(26))
        choice = rng.randrange(3)
        if choice == 0:
            f3 = rng.randrange(8)
            words += [r(rd,a,b,f3,32 if f3 in (0,5) and rng.randrange(2) else 0)]
        elif choice == 1:
            size = rng.randrange(3); offset = rng.randrange(16)*(1 << size)
            words += [store(28,a,offset,size), i(rd,28,offset,size+4 if size<2 and rng.randrange(2) else size,3)]
        else:
            words += [branch(a,b,8,rng.choice((0,1,4,5,6,7))), i(rd,rd,1)]
    # A final store makes completion depend on draining a committed write.
    if self_modifying: words += [store(28,27,4,2)]
    words += [branch(0,0,0)]
    image = bytearray(0x400+4*len(words))
    handler = [csr(30,0,0x342,2),i(31,0,1),branch(30,31,12,1),csr(0,29,0x341,1),0x30200073,
               csr(30,0,0x341,2),i(30,30,4),csr(0,30,0x341,1),0x30200073]
    for base, stream in ((0,boot),(0x100,handler),(0x200,[i(10,0,0),i(0,1,0,0,0x67)]),(0x400,words)):
        for n,word in enumerate(stream): struct.pack_into('<I',image,base+4*n,word)
    return bytes(image), boot, 0x400+4*(len(words)-1)
