`default_nettype none
module cache_storage_pair_probe_tb #(
  parameter int I_BYTES = 1024, D_BYTES = 1024, WAYS = 2, BANK_WORDS = 8,
  localparam int DATA_BITS = BANK_WORDS * 32
);
  logic clk = 0, rst;
  logic [1:0] en, we, touch, way, valid, dirty;
  logic [63:0] address, victim;
  logic [2*DATA_BITS-1:0] wdata, rdata;
  logic [2*DATA_BITS/8-1:0] mask;
  wire [1:0] response, hit, dirty_out, selected;
  string input_path, output_path;
  int source, sink;
  cache_storage_pair_probe #(.I_BYTES(I_BYTES), .D_BYTES(D_BYTES), .WAYS(WAYS), .BANK_WORDS(BANK_WORDS)) dut (
    .clk_i(clk), .rst_i(rst), .en_i(en), .write_i(we), .touch_i(touch), .way_i(way),
    .valid_i(valid), .dirty_i(dirty), .address_i(address), .wdata_i(wdata), .wmask_i(mask),
    .response_o(response), .hit_o(hit), .dirty_o(dirty_out), .way_o(selected),
    .victim_address_o(victim), .rdata_o(rdata)
  );
  initial begin
    if (!$value$plusargs("input=%s", input_path) || !$value$plusargs("output=%s", output_path)) $fatal(1, "paths required");
    source = $fopen(input_path, "r");
    sink = $fopen(output_path, "w");
    if (source == 0 || sink == 0) $fatal(1, "cannot open replay files");
    while ($fscanf(source, "%h %h %h %h %h %h %h %h %h %h", rst, en, we, touch, way,
        address, wdata, mask, valid, dirty) == 10) begin
      #1 clk = 1;
      #1 clk = 0;
      $fdisplay(sink, "%h %h %h %h %h %h", response, hit, dirty_out, selected, victim, rdata);
    end
    $fclose(source);
    $fclose(sink);
    $finish;
  end
endmodule
`default_nettype wire
