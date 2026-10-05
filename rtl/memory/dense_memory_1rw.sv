`default_nettype none
// One synchronous read or masked write per cycle; read data is valid after a read edge.
module dense_memory_1rw #(
  parameter int DEPTH = 16,
  parameter int WIDTH = 128,
  parameter bit MASKED = 1'b1,
  localparam int LANES = MASKED ? WIDTH / 8 : 1,
  localparam int ADDR_BITS = $clog2(DEPTH)
) (
  input  wire                 clk_i,
  input  wire                 en_i,
  input  wire                 we_i,
  input  wire [ADDR_BITS-1:0] addr_i,
  input  wire [WIDTH-1:0]     wdata_i,
  input  wire [LANES-1:0]     wmask_i,
  output logic [WIDTH-1:0]    rdata_o
);
  localparam int GRANULE = WIDTH / LANES;

  if (DEPTH < 16 || DEPTH > 1024 || (DEPTH & (DEPTH - 1)) != 0
      || WIDTH < 1 || WIDTH > 128 || (MASKED && WIDTH % 8 != 0)) begin : g_illegal_shape
    $error("dense_memory_1rw: illegal shape");
  end

  // Payload has no reset; enable low ignores every other input.
  logic [WIDTH-1:0] array_q [DEPTH];

  always_ff @(posedge clk_i) begin
    if (en_i && we_i) begin
      for (int lane = 0; lane < LANES; lane++)
        if (wmask_i[lane])
          array_q[addr_i][lane*GRANULE +: GRANULE] <= wdata_i[lane*GRANULE +: GRANULE];
    end else if (en_i) begin
      rdata_o <= array_q[addr_i];
    end
  end

`ifndef SYNTHESIS
  always_ff @(posedge clk_i) begin
    assert (!en_i || !$isunknown({we_i, addr_i}))
      else $fatal(1, "DENSE_MEMORY_ILLEGAL_REQUEST");
    assert (!(en_i && we_i) || !$isunknown(wmask_i))
      else $fatal(1, "DENSE_MEMORY_ILLEGAL_MASK");
  end
`endif
endmodule
`default_nettype wire
