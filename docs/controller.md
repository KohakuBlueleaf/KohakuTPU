## Controller

### Compute Unit Controller

In this section we discuss about the designation of instruction format and functionality of compute unit controller

#### Functionality

We split instructions into 7 type:

1. computation
2. memory read
3. memory write
4. NoC send
5. NoC recv
6. buffer copy
7. send signal (to global controller)

#### Instructions Format and Design

* Computation

  * 000/001 | OP | nested loop | A start addr | A len | B, C start addr | B, C len | OUT start addr | OUT buf choice
  * 000/001: convert output to FP8 or not, if convert output to FP8, only lower half of output buffer will be
  * OP: 7bit: tensor/alu | alu opmode
  * nested loop: 1bit: flag, use nested loop or zip loop mode
  * start addr: 9bit: the start point of reading/writing buffer
  * len: 9bit: how many data to use in this instruction
  * buf choice: 2bit (4buffer)
  * at least 58bit
* Memory read

  * 010 | ram start addr | length | buf choice | start addr
  * ram start addr: 26bit(34 for 16GB, -6 for 256bit align)
  * at least 49bit
* Memory write

  * 011 | ram start addr | length | buf choice | start addr | half
  * half: 1bit: only use lower half bits of buffer, be used if previous output is FP8
  * at least 50bit
* NoC send

  * 100 | target unit | length | buf choice | start addr
  * target unit: 8bit, NoC ID
  * at least 31bit
* NoC recv

  * 101 | source unit | length | buf choice | start addr
  * NoC send/recv should be a pair (on different unit)
  * at least 31bit
* buffer copy

  * 110 | source buf | dest buf | length | src start | dest start | src half | dest half
  * src half: 2bit: 00 or 11 means full read, 01/ 10 means read upper/lower half
  * dest half: 1bit: if src half is 01/10, dest half indicate writing to upper/lower half
  * at least 37bit
* send signal

  * 111 | NoC id | content
  * indicate a series of instruction is done

#### Details

All the instructions are stored in a FIFO and all instruction are blocked instruction. Since each instruction can contain operation which need at most 512x512 cycles. the wasted cycle for decode/process instructions is not a big deal.

All the instructions are sent by global controller

This instruction set also allow standalone usage (you only need to implement fake NoC mesh and implement a global controller) If needed.

Since ram/memory related instruction in compute unit have smallest unit as 128bit. we will need to implement some standalone memory management unit to do things like reshape, transpose or tiling.

#### Implementation

To simplify the implementation of controller, we split it into following parts

* NoC receiver
* Instruction FIFO
* Buffers
* Control State Machine
  * Instruction Decoder
  * Buffer Mover SM
  * Compute SM
  * Memory Read SM
  * Memory Write SM
  * NoC Send SM
  * NoC Recv SM

To improve efficiency, we allow some different state machine to run together, we split the operation into 3 group:

1. operation: compute/buffer move
2. read: memory write/NoC send
3. recv: memory read/NoC recv

Since operation also need "read" of buffer, we only allow recv group to be taken right after operation group.

For recv group, the NoC receiver will wait until "read" state of buffer is 0 than start writing. (or it will enable back-preasure to NoC mesh)

Operation group have highest priority, it will wait until all previous instruction finished than start executing.

##### TL;DR

* operation -> read: blocked
* operation -> recv: state machine started but enable NoC back preasure until operation done.
  * backpreasure if address overlapped, instruction should have flag for this
* read -> recv: state machine started but enable NoC back preasure if address overlapped
* read -> operation: blocked if address overlapped, instruction should have flag for it
* recv -> read: concurrent executing
* recv -> operation: blocked
* Maximumlly 2 concurrent instruction

### Global Controller

#### Functionality

* Decode high level instructions (if needed)
* Send instructions to each compute unit,
* check return signal from compute units
* communicate with host computer

### Memory Manager

#### Functionality

* Read/Write based on compute unit's request
* memory alignment management (reshape, transpose, contiguous)
