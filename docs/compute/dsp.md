## DSP48E2

For DSP48E1, we can't do M + C + PCIN in one pass, need to move more addition outside the DSP.
But the basic idea about use multiplier and alu for 4fp8 mul in one pass is same.

We will basically only use this operation:
(A(27) + D(27)) * B(18) + C(48)  = output
Where (xx) stands for bit count

### Pipeline

In DSP48E2, we have some pipeline register to make the DSP become staged. Whcih can increase the Fmax.
In our project, we want to achieve at least 400MHz in the Matmul unit, IN THEORY, we don't need any pipeline register for it in -2 or -3 speed grade chip. BUT if needed, we will need to implement pipelining for the unit. (But since the matmul unit itself already need multiple cycle to run the matmul,  it is easy to just add some "latency" for receiving output from fp8 vector mul unit which achieve the pipelining easily.)

pipeline inside DSP48E2 (for A and B we have A1/A2 or B1/B2, but in our project we barely use them)

```
Input| input reg |         Pipeline Reg           | Output Reg

A ----> A reg \
               (Add)=> AD reg\
D ----> D reg /               \
                              (Mult)=> M reg \
B ----> B reg ________________/              (ALU)=> P reg => 
                                             /
C ----> C reg ______________________________/
```

Since not all the input have same amount of reg in the path, we need to add some extra reg to match their cycle, also the Preg can be removed since we directly write the output into our own output reg:

```
A ----> A reg \
               (Add)=> AD reg\
D ----> D reg /               \
                              (Mult)=> M reg \
Breg1 -> B -> B reg __________/              (ALU) => Ouptut wire
                                             /
Creg1 -> Creg2 -> C -> C reg _______________/

ID   -----> reg1 -----> reg2 -----> reg3 =======> ID wire
```

In FP8 Mul we don't need AD preadder, which means we can disable AD reg and shrink the pipeline:

```
A ----> A reg \
               (Mult)=> M reg \
B ----> B reg /              (ALU)=> P reg \
                              /             => Final Output
Creg1 -> C -> C reg _________/             /
                                          /
Inputs -> InpReg1 -> InpReg2 -> InpReg3__/

ID   -----> reg1 -----> reg2 ------> reg3 ======> ID wire
```

**Note**: It is unclear that if we need Preg in the pipelining, but based on my understanding, the Preg can be seen as "write back stage", which means if we connect the Output wire to some reg directly we can ignore it. But since we have some complex operation after the dsp output. it is possible that we should better put some reg behind it as well...
**Note2**: If we use full pipeline we mentioned here, we can achieve around 800, 700, 600MHz in FP8VectorMul unit in speedgrade -3, -2, -1(-2 LE).
